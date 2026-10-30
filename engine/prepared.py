"""Prepared sheets and the request database: every question runs on one private SQLite database.

A question used to run on PostgreSQL: each conversation had a schema there holding its uploaded tables and the
bridges that matched their cells to knowledgebase entities, rebuilt and re-checked by every question, behind a
per-conversation lock. A question cost about 67 statements (0.87 s of 7.5 s, 2026-10-27), and typing and matching
the same cells again cost more. Now:

* ``RequestDatabase`` is the request's private SQLite database. ``main`` holds the request's tables as the planner
  types them, the bridges its routes build and the hybrid search's vectors; ``knowledgebase`` holds the part of the
  knowledgebase the request can reach. The readers' candidate pool, the decomposition probe, the deterministic plan
  of every route, the world path's checks, the hybrid search and the resolution slides all read it. Nothing else
  writes it, so no question waits for another.
* ``TablePreparation`` is what one table needs from the knowledgebase, computed once per version of the table:
  its column routes and typing evidence, every candidate entity of each distinct cell of a routed column (a cell
  can name several Parises, and the country a question names chooses among them), the entity type and matches of
  a column of non-geographic entities, and the knowledgebase rows and labels those reach. It is computed in the
  background when a client syncs its sheets, else by the first question, kept in ``chat.prepared_table`` and cached
  in the process.
* ``Knowledge`` is the instance's view of the shared knowledgebase: the base image (the small tables whole, the
  column catalog, the maintenance schedule, the labels of countries, continents and states), exact-name lookups of a
  question's words, the vectors of the small types for fuzzy question words, and the exchange rates a conversion
  reads.

What stays on PostgreSQL at question time: conversation records, saved references, exact-name lookups of new
question words (cached), exchange rates for a conversion (cached), labels missing from the image, and the
nearby-places search over public.settlement, which reads no upload.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sqlite3
import threading
import time
import zlib
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np

from engine import request_timing
from engine.numeric import canonical_decimal, register_sqlite_decimal, sqlite_numeric

# The preparation's own format. Raise it whenever what a preparation holds, or how it is computed, changes: a
# stored preparation of another format is computed again.
PREPARATION_FORMAT = 2
# The knowledgebase tables every request database holds whole: a few hundred rows each.
WHOLE_TABLES = ("country", "Countries", "States", "u_s_state", "Elements", "Continents", "schedule", "types")
# The knowledgebase tables whose rows arrive with a table's preparation (the entities its cells name) or a
# question's conversion (the rates of its dates). They exist in every request database, empty until then.
REACHED_TABLES = ("city", "Cities", "exchange_rate")
# The knowledgebase's friendly views over its tables, as the world registry names them.
VIEWS = {"Cities in the World": "Cities", "Countries in the World": "Countries", "States in the World": "States",
         "Elements in the World": "Elements", "Continents in the World": "Continents"}
# The entity types whose names a question's words are matched against in memory, by their vectors.
SMALL_TYPES = ("country", "continent", "state")
# How long the world's version is trusted before it is read again: the knowledgebase refreshes daily.
VERSION_SECONDS = 60.0
# In-process caches, by size: prepared tables and exchange rates are kept until these many bytes are held.
PREPARATION_CACHE_BYTES = 256 * 1024 * 1024
RATES_CACHE_BYTES = 64 * 1024 * 1024
NAMES_CACHE_ENTRIES = 200_000
# Preparations kept per conversation in chat.prepared_table: its sheets, saved references and their versions.
STORED_PER_CONVERSATION = 32
# How many SQLite VM steps run between two checks of the request's deadline.
DEADLINE_STEPS = 20_000

# PostgreSQL column types as the request database stores them. A numeric is canonical decimal text, ordered by
# the decimal collation and computed by the decimal functions (engine.numeric); a double precision stays a binary
# float, which both deterministic programs read as its shortest decimal; dates and timestamps are ISO text.
_STORAGE = {
    "text": "TEXT", "character varying": "TEXT", "character": "TEXT", "uuid": "TEXT",
    "smallint": "INTEGER", "integer": "INTEGER", "bigint": "INTEGER", "boolean": "INTEGER",
    "numeric": "TEXT COLLATE decimal", "double precision": "REAL", "real": "REAL",
    "date": "TEXT", "timestamp with time zone": "TEXT", "timestamp without time zone": "TEXT",
    "json": "TEXT", "jsonb": "TEXT",
}


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


# ---------------------------------------------------------------------------------------------------------------
# The SQL functions every request database has
# ---------------------------------------------------------------------------------------------------------------

def _lower(value):
    """SQL lower() over every alphabet. SQLite's own folds only ASCII, so 'ÉCOLE' never matched 'école'; Python's
    folds as PostgreSQL's lower() did, and as the Python program's LOWER does."""
    if value is None:
        return None
    return (value if isinstance(value, str) else str(value)).lower()


def _upper(value):
    if value is None:
        return None
    return (value if isinstance(value, str) else str(value)).upper()


def _regexp_matches(value, pattern):
    """Whether text contains a match of ``pattern``, ignoring case: PostgreSQL's ``~*``. ``\\y`` is its word
    boundary."""
    if value is None or pattern is None:
        return None
    return int(re.search(str(pattern).replace("\\y", "\\b"), str(value), re.IGNORECASE) is not None)


def _vector_distance(left, right):
    """The cosine distance of two float32 vectors stored as blobs: pgvector's ``<=>``."""
    if left is None or right is None:
        return None
    a = np.frombuffer(left, dtype=np.float32)
    b = np.frombuffer(right, dtype=np.float32)
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return 1.0 - (float(a @ b) / denominator if denominator > 0 else 0.0)


def register_functions(connection):
    """Install the functions the planner's SQL and the deterministic programs use on a SQLite connection."""
    register_sqlite_decimal(connection)
    connection.create_function("lower", 1, _lower, deterministic=True)
    connection.create_function("upper", 1, _upper, deterministic=True)
    connection.create_function("regexp_matches", 2, _regexp_matches, deterministic=True)
    connection.create_function("vector_distance", 2, _vector_distance, deterministic=True)


def connect():
    """A new in-memory SQLite database with the functions the planner's SQL and the deterministic programs use."""
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    register_functions(connection)
    return connection


def vector_blob(vector):
    return np.asarray(vector, dtype=np.float32).tobytes()


def stored_value(value):
    """A PostgreSQL value as the request database stores it (see _STORAGE)."""
    if value is None or isinstance(value, (str, int, float, bytes)) and not isinstance(value, bool):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


_NUMERIC = None


def decimal_cursor(connection):
    """A cursor that reads NUMERIC as Decimal: the process reads it as a wire value (engine.pg), and a stored
    decimal must keep every digit."""
    global _NUMERIC
    import psycopg2.extensions
    if _NUMERIC is None:
        _NUMERIC = psycopg2.extensions.new_type(
            (1700,), "PREPARED_NUMERIC", lambda value, _cursor: None if value is None else Decimal(value))
    cursor = connection.cursor()
    psycopg2.extensions.register_type(_NUMERIC, cursor)
    return cursor


def _create(connection, schema, table, columns):
    """Create ``schema``.``table`` with ``columns`` [(name, PostgreSQL type)] stored as _STORAGE says."""
    declarations = ", ".join(f"{_q(name)} {_STORAGE.get(data_type, 'TEXT')}" for name, data_type in columns
                             if data_type in _STORAGE)
    connection.execute(f"CREATE TABLE IF NOT EXISTS {_q(schema)}.{_q(table)} ({declarations})")


def _insert(connection, schema, table, columns, rows):
    if not rows:
        return
    placeholders = ",".join("?" * len(columns))
    connection.executemany(
        f"INSERT INTO {_q(schema)}.{_q(table)} ({', '.join(_q(c) for c in columns)}) VALUES ({placeholders})",
        ([stored_value(value) for value in row] for row in rows))


# ---------------------------------------------------------------------------------------------------------------
# The shared knowledgebase, as this instance reads it at question time
# ---------------------------------------------------------------------------------------------------------------

def _model_identity():
    """The models and code a preparation depends on: the promoted bundle's manifest and the upstream revisions."""
    from engine import model_revisions
    from engine.config import DATA_DIR, kb_model_route_enabled
    revisions = sorted((key, str(value)) for key, value in vars(model_revisions).items() if key.isupper())
    try:
        manifest = hashlib.sha256((Path(DATA_DIR) / "weights_manifest.json").read_bytes()).hexdigest()
    except OSError:
        manifest = ""
    return f"{revisions}|{manifest}|route={int(kb_model_route_enabled())}"


class Knowledge:
    """This instance's reads of the shared knowledgebase (see the module docstring)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._versions = None                  # (read at, (world version, entity version))
        self._base = None                      # the BaseImage of the current world version
        self._base_lock = threading.Lock()
        self._names = OrderedDict()            # (entity version, norm) -> {type: frozenset(qids)}
        self._labels = OrderedDict()           # (entity version, qid) -> label or None
        self._rates = OrderedDict()            # (world version, columns, dates) -> rows
        self._rates_bytes = 0
        self._identity = None

    # ---- versions ----
    def identity(self):
        if self._identity is None:
            self._identity = _model_identity()
        return self._identity

    def versions(self):
        """(world version, entity version). The world version moves with any knowledgebase refresh, the daily
        exchange rates included; the entity version only with the tables entity matching reads, so a prepared
        table outlives a day's new rates. Both carry the model identity."""
        now = time.monotonic()
        with self._lock:
            if self._versions is not None and now - self._versions[0] < VERSION_SECONDS:
                return self._versions[1]
        from engine.pg import _pg
        connection = _pg()
        try:
            cursor = connection.cursor()
            cursor.execute("SELECT to_regclass('knowledgebase.schedule')")
            if cursor.fetchone()[0] is None:
                world = entity = ""
            else:
                cursor.execute(
                    "SELECT COALESCE(max(last_refreshed_at)::text, ''), "
                    "COALESCE(max(last_refreshed_at) FILTER (WHERE table_name <> 'exchange_rate')::text, '') "
                    'FROM knowledgebase."schedule"')
                world, entity = cursor.fetchone()
        finally:
            connection.close()
        identity = self.identity()
        versions = (f"{world}|{identity}", f"{entity}|{identity}")
        with self._lock:
            self._versions = (now, versions)
        return versions

    # ---- the base image ----
    def base(self):
        """The BaseImage of the current world version, built once per version (one build at a time)."""
        world, _entity = self.versions()
        base = self._base
        if base is not None and base.version == world:
            return base
        with self._base_lock:
            base = self._base
            if base is None or base.version != world:
                with request_timing.span("prepare_base"):
                    base = self._base = BaseImage.build(world)
        return base

    # ---- exact names ----
    def names_by_type(self, norms):
        """{norm: {type: {qid}}} for the normalized names in ``norms`` that name a world entity exactly. A name is
        read from PostgreSQL once per entity version; a question's words repeat across turns."""
        norms = sorted({n for n in norms if n})
        if not norms:
            return {}
        _world, entity = self.versions()
        found, missing = {}, []
        with self._lock:
            for norm in norms:
                hit = self._names.get((entity, norm))
                if hit is None:
                    missing.append(norm)
                else:
                    self._names.move_to_end((entity, norm))
                    if hit:
                        found[norm] = hit
        if missing:
            from engine.pg import _pg
            rows = {}
            connection = _pg()
            try:
                cursor = connection.cursor()
                cursor.execute('SELECT norm, type, qid FROM knowledgebase."words" '
                               'WHERE norm = ANY(%s) AND qid IS NOT NULL', (missing,))
                for norm, type_, qid in cursor.fetchall():
                    rows.setdefault(norm, {}).setdefault(type_, set()).add(qid)
            finally:
                connection.close()
            request_timing.count("names_read", len(missing))
            with self._lock:
                for norm in missing:
                    value = {type_: frozenset(qids) for type_, qids in rows.get(norm, {}).items()}
                    self._names[(entity, norm)] = value
                    if value:
                        found[norm] = value
                while len(self._names) > NAMES_CACHE_ENTRIES:
                    self._names.popitem(last=False)
        return {norm: {type_: set(qids) for type_, qids in types.items()} for norm, types in found.items()}

    # ---- fuzzy question words ----
    def nearest(self, vector, type_):
        """(qid, cosine) of the ``type_`` entity whose name is nearest ``vector``, exactly, for the small types;
        (None, -1.0) otherwise."""
        return self.base().nearest(vector, type_)

    # ---- labels ----
    def labels(self, qids, database=None):
        """qid -> label for ``qids``: the request database's labels (the base image's and its tables'
        preparations'), then PostgreSQL for the rest, cached (engine.entities.EntityQuery._qid_labels)."""
        qids = sorted({q for q in qids if q})
        out = {}
        if database is not None:
            out.update(database.labels(qids))
        missing = [q for q in qids if q not in out]
        if not missing:
            return out
        _world, entity = self.versions()
        unknown = []
        with self._lock:
            for qid in missing:
                if (entity, qid) in self._labels:
                    label = self._labels[(entity, qid)]
                    if label is not None:
                        out[qid] = label
                else:
                    unknown.append(qid)
        if unknown:
            fetched = read_labels(unknown)
            with self._lock:
                for qid in unknown:
                    self._labels[(entity, qid)] = fetched.get(qid)
                while len(self._labels) > NAMES_CACHE_ENTRIES:
                    self._labels.popitem(last=False)
            out.update(fetched)
        return out

    # ---- exchange rates ----
    def exchange_rates(self, columns, dates):
        """The exchange_rate rows of ``dates`` (ISO days) with ``columns`` beside the currency and the day, read
        once per world version and kept by size."""
        world, _entity = self.versions()
        known = {name for name, _type in self.base().catalog.get("exchange_rate", ())}
        selected = tuple(sorted(name for name in set(columns) if name in known))
        key = (world, selected, tuple(sorted(dates)))
        with self._lock:
            hit = self._rates.get(key)
            if hit is not None:
                self._rates.move_to_end(key)
                return hit
        from engine.pg import _pg
        connection = _pg()
        try:
            cursor = decimal_cursor(connection)
            cursor.execute(
                "SELECT currency_code, date" + "".join(", " + _q(name) for name in selected)
                + ' FROM knowledgebase."exchange_rate" WHERE date = ANY(%s::date[]) ORDER BY date, currency_code',
                (list(key[2]),))
            rows = ("currency_code", "date") + selected, [tuple(row) for row in cursor.fetchall()]
        finally:
            connection.close()
        size = 64 * len(rows[1]) * (2 + len(selected))
        with self._lock:
            self._rates[key] = rows
            self._rates_bytes += size
            while self._rates_bytes > RATES_CACHE_BYTES and len(self._rates) > 1:
                _key, (columns_, old) = self._rates.popitem(last=False)
                self._rates_bytes -= 64 * len(old) * len(columns_)
        return rows


_KNOWLEDGE = Knowledge()


def knowledge():
    """The instance's one Knowledge."""
    return _KNOWLEDGE


def read_labels(qids):
    """qid -> label from PostgreSQL: the qid's primary name, then the name its source record gives itself, then the
    earliest (the one rule every trail and answer shows a knowledgebase entity by, 2026-10-02)."""
    from engine.pg import _pg
    qids = sorted({q for q in qids if q})
    if not qids:
        return {}
    connection = _pg()
    try:
        cursor = connection.cursor()
        cursor.execute(
            'SELECT DISTINCT ON (qid) qid, canonical FROM knowledgebase."words" '
            'WHERE qid = ANY(%s) AND canonical IS NOT NULL '
            "ORDER BY qid, is_primary IS TRUE DESC, canonical = props->>'name' IS TRUE DESC, id", (qids,))
        return dict(cursor.fetchall())
    finally:
        connection.close()


class BaseImage:
    """The knowledgebase tables every request database starts from, as one serialized SQLite database, with the
    column catalog of every knowledgebase table and the small types' name vectors."""

    def __init__(self, version, image, catalog, vectors):
        self.version = version
        self.image = image                  # bytes of the serialized database
        self.catalog = catalog              # table -> ((column, PostgreSQL type), ...) in column order
        self._vectors = vectors             # type -> (qids, unit matrix)

    @classmethod
    def build(cls, version):
        from engine.pg import _pg
        connection = _pg()
        try:
            cursor = decimal_cursor(connection)
            cursor.execute("SELECT table_name, column_name, data_type FROM information_schema.columns "
                           "WHERE table_schema = 'knowledgebase' ORDER BY table_name, ordinal_position")
            catalog = {}
            for table, column, data_type in cursor.fetchall():
                catalog.setdefault(table, []).append((column, data_type))
            catalog = {table: tuple(columns) for table, columns in catalog.items()}
            database = sqlite3.connect(":memory:")
            register_sqlite_decimal(database)
            database.execute('CREATE TABLE "_catalog" ("table_name" TEXT, "column_name" TEXT, "data_type" TEXT, '
                             '"ordinal" INTEGER)')
            database.executemany('INSERT INTO "_catalog" VALUES (?, ?, ?, ?)',
                                 [(table, column, data_type, index)
                                  for table, columns in sorted(catalog.items())
                                  for index, (column, data_type) in enumerate(columns, 1)])
            for table in WHOLE_TABLES + REACHED_TABLES:
                columns = catalog.get(table)
                if not columns:
                    continue
                _create(database, "main", table, columns)
                if table in WHOLE_TABLES:
                    stored = [name for name, data_type in columns if data_type in _STORAGE]
                    cursor.execute(f"SELECT {', '.join(_q(name) for name in stored)} "
                                   f"FROM knowledgebase.{_q(table)}")
                    _insert(database, "main", table, stored, cursor.fetchall())
            for view, table in VIEWS.items():
                if table in catalog and view in catalog:
                    names = ", ".join(_q(name) for name, data_type in catalog[view] if data_type in _STORAGE)
                    database.execute(f"CREATE VIEW {_q(view)} AS SELECT {names} FROM {_q(table)}")
            cursor.execute(
                'SELECT DISTINCT ON (qid) qid, canonical FROM knowledgebase."words" '
                "WHERE type IN ('country', 'continent', 'state', 'element') AND qid IS NOT NULL "
                "AND canonical IS NOT NULL "
                "ORDER BY qid, is_primary IS TRUE DESC, canonical = props->>'name' IS TRUE DESC, id")
            database.execute('CREATE TABLE "_labels" ("qid" TEXT PRIMARY KEY, "label" TEXT)')
            database.executemany('INSERT INTO "_labels" VALUES (?, ?)', cursor.fetchall())
            cursor.execute('SELECT type, qid, embedding::text FROM knowledgebase."words" '
                           'WHERE type = ANY(%s) AND qid IS NOT NULL AND embedding IS NOT NULL '
                           'ORDER BY type, qid, id', (list(SMALL_TYPES),))
            grouped = {}
            for type_, qid, text in cursor.fetchall():
                grouped.setdefault(type_, ([], []))
                grouped[type_][0].append(qid)
                grouped[type_][1].append(np.asarray(json.loads(text), dtype=np.float32))
            vectors = {}
            for type_, (qids, rows) in grouped.items():
                matrix = np.vstack(rows)
                matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9)
                vectors[type_] = (qids, matrix)
            database.commit()
            image = database.serialize()
            database.close()
        finally:
            connection.close()
        return cls(version, image, catalog, vectors)

    def nearest(self, vector, type_):
        entry = self._vectors.get(type_)
        if entry is None:
            return None, -1.0
        qids, matrix = entry
        query = np.asarray(vector, dtype=np.float32)
        query = query / max(float(np.linalg.norm(query)), 1e-9)
        scores = matrix @ query
        best = int(np.argmax(scores))          # the first of equal scores: rows are ordered by qid, then id
        return qids[best], float(scores[best])


# ---------------------------------------------------------------------------------------------------------------
# The request database
# ---------------------------------------------------------------------------------------------------------------

_DATABASE: ContextVar = ContextVar("request_database", default=None)


class RequestDatabase:
    """The request's private SQLite database (see the module docstring)."""

    def __init__(self, base=None):
        self.connection = connect()
        self._base = base
        self._tables = {}                    # main table -> fingerprint of the rows loaded into it
        self._fingerprints = {}              # (table name, id(rows)) -> (rows, fingerprint) for this request
        self._prepared = {}                  # preparation key -> the preparation, its rows held
        self._held = {}                      # identity of a request table -> (its rows, its preparation)
        self._reached = {}                   # knowledgebase table -> qids of the rows it holds
        self._labels = {}                    # qid -> label from the preparations
        self._rates = (set(), set())         # (days, columns) the exchange_rate table holds
        self._cities = None                  # (qids, unit matrix) of the city names the preparations matched
        self.connection.execute("ATTACH DATABASE ':memory:' AS knowledgebase")
        if base is not None:
            self.connection.deserialize(base.image, name="knowledgebase")
        self.restore_deadline()

    # ---- the deadline ----
    def restore_deadline(self):
        """End any statement once the request's deadline passes (it was the PostgreSQL statement timeout)."""
        from engine.request_deadline import expired
        self.connection.set_progress_handler(lambda: int(expired()), DEADLINE_STEPS)

    # ---- the request's tables ----
    def load(self, tablemap, sch):
        """The connection, holding every table of ``sch`` in ``main`` as the planner types it: a REAL column as
        canonical decimal text ordered by the decimal collation, an INTEGER one as integers, the rest as text. A
        table already loaded with the same rows is kept."""
        by_table = {}
        for column in sch:
            by_table.setdefault(column["table"], []).append(column)
        changed = False
        for name, columns in by_table.items():
            table = tablemap[name]
            fingerprint = self._fingerprint(name, columns, table)
            if self._tables.get(name) == fingerprint:
                continue
            with request_timing.span("load_tables"):
                self.connection.execute(f"DROP TABLE IF EXISTS main.{_q(name)}")
                declarations = []
                for column in columns:
                    storage = "TEXT COLLATE decimal" if column["affinity"] == "REAL" else column["affinity"]
                    declarations.append(f'{_q(column["name"])} {storage}')
                self.connection.execute(f"CREATE TABLE main.{_q(name)} (" + ", ".join(declarations) + ")")
                position = {column: index for index, column in enumerate(table["columns"])}
                fields = [(position.get(column["name"]), column["affinity"]) for column in columns]
                self.connection.executemany(
                    f"INSERT INTO main.{_q(name)} VALUES ({','.join('?' * len(columns))})",
                    ([_cell(row[index] if index is not None and index < len(row) else None, affinity)
                      for index, affinity in fields] for row in table["rows"]))
            self._tables[name] = fingerprint
            changed = True
        if changed:
            self.connection.commit()
        return self.connection

    def _fingerprint(self, name, columns, table):
        rows = table["rows"]
        key = (name, id(rows))
        hit = self._fingerprints.get(key)
        if hit is not None and hit[0] is rows:
            shape = hit[1]
        else:
            digest = hashlib.sha256(json.dumps(rows, default=str, ensure_ascii=False).encode("utf-8"))
            shape = digest.hexdigest()
            self._fingerprints[key] = (rows, shape)
        return (tuple((column["name"], column["affinity"]) for column in columns), tuple(table["columns"]), shape)

    # ---- the knowledgebase part ----
    def catalog(self, table):
        """((column, PostgreSQL type), ...) of a knowledgebase table, in column order; () when it has none."""
        return self._base.catalog.get(table, ()) if self._base is not None else ()

    @staticmethod
    def _identity(table):
        """A request table's identity for this request: its name, columns and row objects. A question normalizes
        its tables more than once, each time into a new list of the same row lists."""
        rows = table["rows"]
        return (str(table["name"]), tuple(table["columns"]), len(rows),
                id(rows[0]) if rows else 0, id(rows[-1]) if rows else 0)

    def prepared_for(self, table):
        """The preparation this request already holds for ``table``, or None."""
        held = self._held.get(self._identity(table))
        rows = table["rows"]
        if held is None or (rows and (held[0][0] is not rows[0] or held[0][1] is not rows[-1])):
            return None
        return held[1]

    def add_preparation(self, preparation, table=None):
        """Hold the knowledgebase rows and labels ``preparation`` reaches, and remember it for ``table``."""
        if table is not None:
            rows = table["rows"]
            self._held[self._identity(table)] = ((rows[0], rows[-1]) if rows else (None, None), preparation)
        if self._base is None or preparation.key in self._prepared:
            return
        self._prepared[preparation.key] = preparation
        for table_name, block in preparation.reached.items():
            self.add_reached(table_name, block, commit=False)
        self._labels.update(preparation.labels)
        self._cities = None
        self.connection.commit()

    def add_reached(self, table_name, block, labels=None, commit=True):
        """Hold a knowledgebase table's rows ``block`` {"columns", "rows"} and their entities' ``labels``. Each
        entity's rows once, all of them: two tables naming one city hold its rows once, and a city with two rows
        keeps both, as PostgreSQL joined them."""
        if labels:
            self._labels.update(labels)
        if self._base is None or not self.catalog(table_name):
            return
        _create(self.connection, "knowledgebase", table_name, self.catalog(table_name))
        columns = list(block["columns"])
        key = columns.index("qid") if "qid" in columns else None
        held = self._reached.setdefault(table_name, set())
        fresh = [row for row in block["rows"] if key is None or row[key] not in held]
        held.update(row[key] for row in fresh if key is not None)
        known = {name for name, data_type in self.catalog(table_name) if data_type in _STORAGE}
        kept = [index for index, name in enumerate(columns) if name in known]
        self.connection.executemany(
            f"INSERT INTO knowledgebase.{_q(table_name)} ({', '.join(_q(columns[i]) for i in kept)}) "
            f"VALUES ({','.join('?' * len(kept))})",
            ([row[i] for i in kept] for row in fresh))
        if commit:
            self.connection.commit()

    def nearest_city(self, vector):
        """(qid, cosine) of the city name nearest ``vector`` among the names the request's tables matched."""
        if self._cities is None:
            names = sorted({(norm, qid): row for preparation in self._prepared.values()
                            for norm, qid, row in preparation.cities}.items())
            if names:
                matrix = np.vstack([row for _key, row in names]).astype(np.float32)
                matrix /= np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9)
                self._cities = ([qid for (_norm, qid), _row in names], matrix)
            else:
                self._cities = ([], None)
        qids, matrix = self._cities
        if matrix is None:
            return None, -1.0
        query = np.asarray(vector, dtype=np.float32)
        query = query / max(float(np.linalg.norm(query)), 1e-9)
        scores = matrix @ query
        best = int(np.argmax(scores))
        return qids[best], float(scores[best])

    def labels(self, qids):
        out = {qid: self._labels[qid] for qid in qids if qid in self._labels}
        missing = [qid for qid in qids if qid not in out]
        if missing and self._base is not None:
            for start in range(0, len(missing), 500):
                chunk = missing[start:start + 500]
                out.update(self.connection.execute(
                    f'SELECT qid, label FROM knowledgebase."_labels" WHERE qid IN ({",".join("?" * len(chunk))})',
                    chunk).fetchall())
        return out

    def load_rates(self, columns, days):
        """Hold the exchange rates of ``days`` (ISO days) with ``columns``: one row per currency and day."""
        if self._base is None:
            return
        held_days, held_columns = self._rates
        wanted = set(columns) | {"updated_at", "source", "source_release_id"}
        new_days = sorted(set(days) - held_days)
        new_columns = sorted(wanted - held_columns) if held_days else []
        all_columns = sorted(held_columns | wanted)
        if new_days:
            names, rows = knowledge().exchange_rates(all_columns, new_days)
            _insert(self.connection, "knowledgebase", "exchange_rate", list(names), rows)
        if new_columns:
            names, rows = knowledge().exchange_rates(new_columns, sorted(held_days))
            assignments = ", ".join(f"{_q(name)} = ?" for name in names[2:])
            if assignments:
                self.connection.executemany(
                    f'UPDATE knowledgebase."exchange_rate" SET {assignments} '
                    'WHERE "currency_code" = ? AND "date" = ?',
                    ([stored_value(value) for value in row[2:]] + [stored_value(row[0]), stored_value(row[1])] for row in rows))
        self._rates = (held_days | set(new_days), held_columns | wanted)
        self.connection.commit()

    # ---- bridges ----
    def replace_bridge(self, name, column, rows):
        """Hold ``rows`` as the bridge ``name``'s rows for ``column``: (column, value, world_type, world_key,
        country, world_qid, entity_qid, context), the cell-to-entity matches a world join reads."""
        self.connection.execute(
            f'CREATE TABLE IF NOT EXISTS main.{_q(name)} ("column" TEXT, "value" TEXT, "world_type" TEXT, '
            '"world_key" TEXT, "country" TEXT, "world_qid" TEXT, "entity_qid" TEXT, "context" TEXT)')
        self.connection.execute(f'DELETE FROM main.{_q(name)} WHERE "column" = ?', (column,))
        self.connection.executemany(f"INSERT INTO main.{_q(name)} VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.connection.commit()

    def has_table(self, name, schema="main"):
        return self.connection.execute(
            f"SELECT 1 FROM {_q(schema)}.sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (name,)).fetchone() is not None

    def select_in(self, sql_head, values, chunk=500):
        """Rows of ``sql_head`` (a SELECT ending in ``IN``) over ``values``, read in chunks."""
        values = list(values)
        rows = []
        for start in range(0, len(values), chunk):
            part = values[start:start + chunk]
            rows.extend(self.connection.execute(f"{sql_head} ({','.join('?' * len(part))})", part).fetchall())
        return rows

    def close(self):
        self.connection.close()


def _cell(value, affinity):
    if value is None:
        return None
    if affinity in ("INTEGER", "REAL"):
        return sqlite_numeric(value, affinity)
    return str(value)


@contextmanager
def request_database():
    """Serve one request on its own database: the knowledgebase's base image attached, the request's tables loaded
    as it reads them. Nested calls share the outer request's database."""
    if _DATABASE.get() is not None:
        yield _DATABASE.get()
        return
    with request_timing.span("prepare_database"):
        database = RequestDatabase(knowledge().base())
    token = _DATABASE.set(database)
    try:
        yield database
    finally:
        _DATABASE.reset(token)
        database.close()


def current_database():
    """The request's database, or None outside a served request."""
    return _DATABASE.get()


@contextmanager
def scratch_database():
    """A database for one call outside a served request (an evaluator, a hermetic test): the caller's tables
    and no knowledgebase."""
    current = _DATABASE.get()
    if current is not None:
        yield current
        return
    database = RequestDatabase(None)
    token = _DATABASE.set(database)
    try:
        yield database
    finally:
        _DATABASE.reset(token)
        database.close()


# ---------------------------------------------------------------------------------------------------------------
# Table preparations
# ---------------------------------------------------------------------------------------------------------------

def table_key(table):
    """The identity of a table's content as preparation reads it: its name, columns and rows, without the columns
    a dataset op synthesizes (a currency claim's constant code, which no route reads)."""
    from engine.dataset_semantics import is_synthetic_currency_column
    kept = [index for index, column in enumerate(table["columns"]) if not is_synthetic_currency_column(column)]
    payload = [str(table["name"]), [str(table["columns"][i]) for i in kept],
               [[row[i] if i < len(row) else None for i in kept] for row in table["rows"]]]
    return hashlib.sha256(json.dumps(payload, default=str, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class TablePreparation:
    """What one table needs from the knowledgebase (see the module docstring)."""

    def __init__(self, key, version, record):
        self.key = key
        self.version = version
        self.routes = {(table, column): world for table, column, world in record.get("routes", ())}
        self.typing = record.get("typing", [])
        # column -> {"type": world type, "cells": {cell: match}} for each routed column (engine/entities.py)
        self.columns = record.get("columns", {})
        # column -> {"label", "qid", "matches": {cell: qid or None}} for a column of non-geographic entities
        self.nongeo = record.get("nongeo", {})
        # knowledgebase table -> {"columns": [...], "rows": [[...]]}: the rows the matches reach
        self.reached = record.get("reached", {})
        self.labels = record.get("labels", {})
        # What a question read later and kept with the preparation in this process: the nearest names of a
        # non-geographic column's cells that matched no name exactly (KnowledgeQuery._nongeo_matches).
        self.extras = {}
        # (norm, qid, vector) of the city names the cells matched, for a question's fuzzy city words
        self.cities = [(norm, qid, np.frombuffer(base64.b64decode(vector), dtype=np.float16).astype(np.float32))
                       for norm, qid, vector in record.get("cities", ())]
        self.record = record

    def encode(self):
        return zlib.compress(json.dumps(self.record, ensure_ascii=False, separators=(",", ":"),
                                        default=str).encode("utf-8"), 6)

    @classmethod
    def decode(cls, key, version, blob):
        return cls(key, version, json.loads(zlib.decompress(blob).decode("utf-8")))

    def size(self):
        if getattr(self, "_size", None) is None:
            self._size = len(json.dumps(self.record, default=str)) + 2048 * len(self.cities)
        return self._size


def encode_city_vector(vector):
    return base64.b64encode(np.asarray(vector, dtype=np.float16).tobytes()).decode("ascii")


class Preparations:
    """Table preparations, from this process's cache, chat.prepared_table, or computed (see the module docstring)."""

    def __init__(self):
        self._cache = OrderedDict()            # (key, version) -> TablePreparation
        self._bytes = 0
        self._lock = threading.Lock()
        self._running = {}                     # (key, version) -> Event of the computation in progress

    def get(self, resolver, table, conversation=None):
        """The preparation of ``table``: cached, stored for ``conversation``, else computed by ``resolver`` (the
        world planner, whose models and PostgreSQL reads type and match the cells) and stored."""
        key = table_key(table)
        _world, version = knowledge().versions()
        version = f"{PREPARATION_FORMAT}|{version}"
        slot = (key, version)
        while True:
            with self._lock:
                hit = self._cache.get(slot)
                if hit is not None:
                    self._cache.move_to_end(slot)
                    request_timing.count("prepared_cached")
                    return hit
                running = self._running.get(slot)
                if running is None:
                    running = self._running[slot] = threading.Event()
                    owner = True
                else:
                    owner = False
            if not owner:
                from engine.request_deadline import remaining
                left = remaining()                      # raises once the request's budget is spent
                with request_timing.span("prepare_wait"):
                    running.wait(timeout=min(600.0, left) if left is not None else 600.0)
                continue
            try:
                preparation = self._read_stored(conversation, key, version)
                if preparation is None:
                    with request_timing.span("prepare_table"):
                        preparation = TablePreparation(key, version, resolver.prepare_table(table))
                    request_timing.count("prepared_computed")
                    if conversation:
                        self._store(conversation, preparation)
                else:
                    request_timing.count("prepared_stored")
                self._keep(slot, preparation)
                return preparation
            finally:
                with self._lock:
                    self._running.pop(slot, None)
                running.set()

    def _keep(self, slot, preparation):
        with self._lock:
            self._cache[slot] = preparation
            self._bytes += preparation.size()
            self._evict()

    def keep_extra(self, preparation, key, value, size):
        """Keep ``value`` with ``preparation`` (TablePreparation.extras), counted against the cache's bytes."""
        with self._lock:
            preparation.extras[key] = value
            preparation._size = preparation.size() + size
            if any(cached is preparation for cached in self._cache.values()):
                self._bytes += size
                self._evict()
        return value

    def _evict(self):
        while self._bytes > PREPARATION_CACHE_BYTES and len(self._cache) > 1:
            _slot, old = self._cache.popitem(last=False)
            self._bytes -= old.size()

    @staticmethod
    def _read_stored(conversation, key, version):
        if not conversation:
            return None
        from engine.pg import _pg
        connection = _pg()
        try:
            cursor = connection.cursor()
            cursor.execute('SELECT record FROM "chat"."prepared_table" '
                           'WHERE conversation_id = %s AND table_key = %s AND preparation_version = %s',
                           (conversation, key, version))
            row = cursor.fetchone()
            connection.commit()
        finally:
            connection.close()
        return TablePreparation.decode(key, version, bytes(row[0])) if row else None

    @staticmethod
    def _store(conversation, preparation):
        """Keep ``preparation`` for ``conversation``: its newest STORED_PER_CONVERSATION preparations stay. A
        preparation is derived data, rebuilt when missing, so it is not counted against the user's storage."""
        from engine.pg import _pg
        blob = preparation.encode()
        connection = _pg()
        try:
            cursor = connection.cursor()
            cursor.execute(
                'INSERT INTO "chat"."prepared_table" (conversation_id, table_key, preparation_version, record, '
                'record_bytes) SELECT %s, %s, %s, %s, %s WHERE EXISTS '
                '(SELECT 1 FROM "chat"."conversation" WHERE conversation_id = %s) '
                'ON CONFLICT (conversation_id, table_key) DO UPDATE SET preparation_version = '
                'EXCLUDED.preparation_version, record = EXCLUDED.record, record_bytes = EXCLUDED.record_bytes, '
                'prepared_at = now()',
                (conversation, preparation.key, preparation.version, blob, len(blob), conversation))
            cursor.execute(
                'DELETE FROM "chat"."prepared_table" WHERE conversation_id = %s AND table_key IN '
                '(SELECT table_key FROM "chat"."prepared_table" WHERE conversation_id = %s '
                'ORDER BY prepared_at DESC, table_key OFFSET %s)',
                (conversation, conversation, STORED_PER_CONVERSATION))
            connection.commit()
        except Exception as error:                       # noqa: BLE001 - a preparation is rebuilt when missing
            connection.rollback()
            print(f"[prepared] store skipped: {type(error).__name__}", flush=True)
        finally:
            connection.close()


_PREPARATIONS = Preparations()


def preparations():
    return _PREPARATIONS


def prepare_sheets_in_background(resolver, tables, conversation):
    """Prepare a conversation's tables after its sheets sync, so its first question finds them prepared. A question
    that arrives first waits for the preparation in progress rather than starting another."""
    def run():
        from engine.request_state import request_scope
        try:
            with request_scope():
                norm, _fks = resolver.ingest(tables)
                for table in norm:
                    preparations().get(resolver, table, conversation)
        except Exception as error:                   # noqa: BLE001 - the first question prepares instead
            print(f"[prepared] background preparation skipped: {type(error).__name__}", flush=True)
        finally:
            try:
                resolver.end_request()
            except Exception:                        # noqa: BLE001
                pass

    worker = threading.Thread(target=run, name="prepare-sheets", daemon=True)
    worker.start()
    return worker
