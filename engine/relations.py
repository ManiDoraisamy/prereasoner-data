"""DETERMINISTIC multi-table relating (no model). Per-table row DEDUP + foreign-key DISCOVERY.

FK detection = classic INCLUSION DEPENDENCY: A.x references B.y when
  - B.y is a candidate KEY (~unique, no nulls),
  - A.x's non-null values are (mostly) a SUBSET of B.y's values (value inclusion),
  - it is many-to-one (A has <= as many distinct values as B's key),
boosted by NAME (`city_id` -> `cities.id`) and TYPE agreement. This is the deterministic graph that drives
the cross-table relational-attention `fk` edge.

Table form: {"name": str, "columns": [str], "rows": [[val, ...], ...]} (rows aligned to columns).
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from math import isfinite
from engine.numeric import parse_decimal, stored_as_numbers

# One served question derives the same tables up to ten times (the decomposition probe, compose, the
# delegate and its fallbacks): a 6-tab, 34,500-row subscriptions workbook spent 52 s of a request on FK
# discovery, and built its planner schema and schema graph four times each (2026-10-04). Each is a pure
# function of the tables' content, so within one request (`request_memo`) a repeat reuses the first result.
#
# A person then asks the same sheet one question after another, and each request derived it again: 5.7 to 8.7 s of
# every question on a 500,000-cell workbook on Cloud Run (2026-10-31). The process keeps the latest derivations too
# (`_KEPT`): the key is the content, so a request only ever reuses what its own cells derive. About 240 bytes a cell
# were measured over a workbook's three derivations, and each derivation counts the workbook's cells: 12,000,000
# keeps about 4,000,000 cells of workbooks, about 1 GB of a 16 GB instance that peaks at 89%.
KEPT_CELLS = 12_000_000
KEPT_SECONDS = 1800
_MEMO = contextvars.ContextVar("request_memo", default=None)
_MISSING = object()
_KEPT = {}                                                     # slot -> (result, cells, last used), oldest use first
_KEPT_LOCK = threading.Lock()


@contextlib.contextmanager
def request_memo():
    """Scope `memoized` reuse to one served request."""
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def _kept(slot):
    """What the process keeps under ``slot``, marked as just used; none past ``KEPT_SECONDS`` unused."""
    now = time.monotonic()
    with _KEPT_LOCK:
        for old, (_, _, used) in list(_KEPT.items()):
            if now - used <= KEPT_SECONDS:
                break
            del _KEPT[old]
        entry = _KEPT.pop(slot, None)
        if entry is None:
            return _MISSING
        _KEPT[slot] = (entry[0], entry[1], now)
    return entry[0]


def _keep(slot, result, cells):
    """Keep ``result``, dropping the least recently used past ``KEPT_CELLS`` in all."""
    if cells > KEPT_CELLS:
        return
    with _KEPT_LOCK:
        _KEPT.pop(slot, None)
        _KEPT[slot] = (result, cells, time.monotonic())
        total = sum(entry[1] for entry in _KEPT.values())
        for old, (_, size, _) in list(_KEPT.items()):
            if total <= KEPT_CELLS:
                break
            del _KEPT[old]
            total -= size


def memoized(kind, key, compute, cells):
    """``compute()``, or within `request_memo` the result an equal ``key()`` of the same kind already computed,
    in this request or one before it (``cells``: how many cells of tables the result holds, for the process's
    bound). A key that cannot be hashed (an unhashable cell) computes afresh. A result is shared: callers do not
    change it."""
    memo = _MEMO.get()
    if memo is None:
        return compute()
    try:
        slot = (kind, key())
        found = memo.get(slot, _MISSING)
    except TypeError:
        return compute()
    if found is _MISSING:
        found = _kept(slot)
        if found is _MISSING:
            found = compute()
            _keep(slot, found, cells)
        memo[slot] = found
    return found


def table_cells(tables):
    """How many cells ``tables`` hold."""
    return sum(len(table["rows"]) * len(table["columns"]) for table in tables)


def content_key(tables):
    """The tables' names, columns and rows, hashable (normalized tables: rows are sequences)."""
    return tuple((t["name"], tuple(t["columns"]), tuple(map(tuple, t["rows"]))) for t in tables)


def _num(v):
    try:
        parse_decimal(v); return True
    except (ValueError, AttributeError):
        return False


def coltype(values):
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return "empty"
    return "num" if sum(_num(v) for v in vals) / len(vals) >= 0.9 else "text"


def cells(t, ci):
    return [row[ci] if ci < len(row) else None for row in t["rows"]]


def join_keys(values):
    """A column's cells as the executed equi-join compares them, None for a blank cell. A column is stored
    the way all its filled cells allow (numeric.stored_as_numbers): a column of numbers joins by exact
    magnitude (1001, '1001' and 1001.0 are one key; 2**53 and 2**53 + 1 are two), and any other column joins
    on each cell's exact text, so in a column that also holds 'ABC', '1.0' and '1' are two keys and 'Abc' is
    not 'abc'. Number keys are Decimals and text keys strings, so a column of numbers never matches a text
    column: PostgreSQL cannot compare BIGINT with TEXT, and the Python emitter does not equate them.
    Foreign-key discovery, reference selection and join grounding (engine/sql_grounding.py) read keys only
    this way: case-folded keys claimed a foreign key whose join matched no row, float magnitudes merged
    distinct integers, and numbers read cell by cell matched '1.0' to '1' in text columns, where the join
    dropped the rows (release reviews, 2026-10-07)."""
    def blank(value):
        return value is None or str(value).strip() == ""
    if stored_as_numbers(values):
        return [None if blank(value) else parse_decimal(value, enforce_input_bounds=False) for value in values]
    return [None if blank(value) else str(value) for value in values]


def is_key(values):
    # A join/FK TARGET must be EXACTLY unique — a 0.98 tolerance let a column with a few duplicate keys become
    # an FK target, so a fact row matched multiple parent rows and the join fanned out (inflating SUM/COUNT/AVG).
    # This strict uniqueness rule is the one both paths use: engine.joins.discover_fks (the compose/SQLite
    # path) delegates here, so the planner and the compose panel share this exact key test.
    return _unique(join_keys(values))


def _unique(keys):
    nn = [key for key in keys if key is not None]
    return len(nn) >= 2 and len(nn) == len(keys) and len(set(nn)) == len(nn)     # unique (exact), no nulls


def _entity_words(tables):
    """The last word of each table's name: what a key's role word names when it names a table."""
    from engine.sql_schema import canon, name_words
    return frozenset(words[-1] for table in tables if (words := [canon(word) for word in name_words(table["name"])]))


def _name_boost(ax, bname, by, entities):
    # Import at use time: sql_schema itself uses this module's layout helper. ``entities`` are the tables' names
    # (_entity_words).
    from engine.sql_schema import SURROGATE_KEY_WORDS, canon, name_words

    source = tuple(canon(word) for word in name_words(ax))
    target = tuple(canon(word) for word in name_words(by))
    entity = tuple(canon(word) for word in name_words(bname))
    # Lowercase compact keys lack a word boundary. Split one only when its
    # prefix exactly names the candidate entity, so "shipid" still links to
    # ships while an ordinary word such as "valid" supplies no "val" role.
    if (len(source) == 1 and source[0].endswith("id") and entity
            and canon(source[0][:-2]) == entity[-1]):
        source = (source[0][:-2], "id")
    key_words = SURROGATE_KEY_WORDS | {"code"}
    suffix = bool(source) and (source[-1] in SURROGATE_KEY_WORDS
                               or len(source) > 1 and source[-1] == "code")
    role = source[:-1] if suffix else source
    width = min(len(role), len(entity))
    # Compare the referenced role's suffix. A student ID cannot name an
    # enrolment ID merely because both namespaces begin with "student".
    # Qualifiers such as "home" and "caused by" may precede the actual role.
    def matches(short, full):
        return short == full or (len(short) >= 3 and len(short) < len(full) and full.startswith(short))

    role_match = bool(width) and all(matches(short, full)
                                     for short, full in zip(role[-width:], entity[-width:]))
    # The referenced field names whose identity it holds: a bridge table's Member_ID is a member, so a branch's key
    # names no reference to it, however the bridge table is named (shop_membership's membership_register_branch,
    # holdout2 411, 2026-10-17: Branch_ID -> Member_ID was discovered and changed every branch join).
    target_suffix = bool(target) and (target[-1] in SURROGATE_KEY_WORDS or len(target) > 1 and target[-1] == "code")
    target_role = target[:-1] if target_suffix else ()
    if role_match and target_role and target_role[-1:] != entity[-1:]:
        target_width = min(len(role), len(target_role))
        role_match = bool(target_width) and all(matches(short, full) for short, full in
                                                zip(role[-target_width:], target_role[-target_width:]))
    # Identically named natural/qualified keys are evidence; bare ID/code are
    # namespaces shared by unrelated tables, so equality of their cells is not.
    same_key = source == target and bool(source) and any(word not in key_words for word in source)
    evidence = 0.35 * role_match + 0.10 * same_key
    if (not evidence and suffix and target_role and target_role[-1:] == entity[-1:] and role
            and role[-1] not in entities):
        # A key named for the role it plays references the entity's own named key: stores.Manager_ID ->
        # employees.Employee_ID (the shipped regression gate, regress/offline_cases.py). The value inclusion
        # decides, and only a repeating column may (a unique one needs the stronger evidence of its name). A role
        # naming another table is that table's: Products.parent_product_id is a product, not a customer
        # (held-out 546, 2026-10-17: its integers fell inside Customers.customer_id).
        return 0.15
    return evidence + (0.15 if evidence and source[-1] in SURROGATE_KEY_WORDS else 0.0)


def layout(columns):
    """A table's layout: its column names, ignoring case, spacing and order. Tables with one layout are
    copies of one export kept per product, month or keyword list."""
    return frozenset(" ".join(str(column).lower().split()) for column in columns)


def discover_fks(tables, min_incl=0.9):
    """The tables' many-to-one foreign keys (the module's inclusion-dependency rule)."""
    found = memoized("fks", lambda: (min_incl, content_key(tables)), lambda: _discover_fks(tables, min_incl),
                     table_cells(tables))
    return [dict(fk) for fk in found]


def _discover_fks(tables, min_incl):
    keys = {}                                                  # (table, col) -> set of its join keys
    column_keys = {}                                           # a column's join keys, read at most once

    def keys_of(table, index):
        if (id(table), index) not in column_keys:
            column_keys[(id(table), index)] = join_keys(cells(table, index))
        return column_keys[(id(table), index)]

    for t in tables:
        for ci, c in enumerate(t["columns"]):
            if _unique(keys_of(t, ci)):
                keys[(t["name"], c)] = {key for key in keys_of(t, ci) if key is not None}
    types = {}                                                 # a column's coltype, read at most once

    def type_of(table, index):
        if (id(table), index) not in types:
            types[(id(table), index)] = coltype(cells(table, index))
        return types[(id(table), index)]

    fks = []
    entities = _entity_words(tables)
    for A in tables:
        for axi, ax in enumerate(A["columns"]):
            # The row-free conditions first; every condition below is required, so their order changes
            # nothing but the cost. Most columns have no candidate parent and never read their cells.
            candidates = []
            for (bname, by), bset in keys.items():
                if bname == A["name"]:
                    continue                                   # a join FK lives ACROSS tables, never same-table
                B = next(t for t in tables if t["name"] == bname)
                if layout(A["columns"]) == layout(B["columns"]):
                    # Tabs with the same columns are copies of one layout (monthly exports, one tab per keyword
                    # list), never parent and child: a customer's Checklist "Avg. monthly searches" was linked to
                    # Inspection's unique values of that measure on the identical name alone (2026-10-02).
                    continue
                # A foreign key needs a NAME/key-name signal, not merely value inclusion. Without one, a repeating
                # measure/flag/low-cardinality-categorical column whose distinct values happen to fall inside an
                # unrelated unique key (qty -> warehouse.wh_id; a 0/1 flag -> a {0,1} lookup; severity -> priority.level)
                # is faked into an FK. Require some name evidence in BOTH cases; a UNIQUE (1:1) child needs a STRONGER
                # signal, since two independent keys overlap by chance more readily than a repeating column does.
                nb = _name_boost(ax, bname, by, entities)
                if nb > 0.0:
                    candidates.append((bname, by, bset, B, nb))
            if not candidates:
                continue
            avals = [key for key in keys_of(A, axi) if key is not None]
            if not avals:
                continue
            aset = set(avals)
            many_to_one = len(aset) < len(avals)               # the FK column REPEATS (it is not itself unique)
            best = None
            for bname, by, bset, B, nb in candidates:
                if not many_to_one and nb < 0.3:
                    continue
                if len(aset) > len(bset):
                    continue                                   # FK side has <= the key's distinct cardinality
                incl = len(aset & bset) / len(aset)
                if incl < min_incl:
                    continue
                if type_of(A, axi) != type_of(B, B["columns"].index(by)):
                    continue
                conf = min(1.0, incl * 0.6 + nb + (0.2 if many_to_one else 0.0))
                if best is None or conf > best[2]:
                    best = (bname, by, conf, incl)
            if best and best[2] >= 0.6:
                fks.append({"from_table": A["name"], "from_col": ax, "to_table": best[0], "to_col": best[1],
                            "conf": round(best[2], 2), "inclusion": round(best[3], 2)})
    return fks


def _explicit_fk(raw, tables):
    if hasattr(raw, "as_foreign_key"):
        raw = raw.as_foreign_key(1.0)
    if not isinstance(raw, dict):
        raise ValueError("explicit foreign keys must be mappings or ExplicitKeyEdge values")
    from_table, to_table = str(raw.get("from_table", "")), str(raw.get("to_table", ""))
    from_cols = raw.get("from_cols", (raw.get("from_col"),))
    to_cols = raw.get("to_cols", (raw.get("to_col"),))
    if isinstance(from_cols, str) or isinstance(to_cols, str):
        raise ValueError("explicit foreign-key columns must be collections")
    from_cols, to_cols = tuple(from_cols), tuple(to_cols)
    if not from_cols or len(from_cols) != len(to_cols) or None in from_cols + to_cols:
        raise ValueError("explicit foreign keys require equally sized, non-empty column tuples")
    columns = {str(table["name"]): set(map(str, table["columns"])) for table in tables}
    if from_table not in columns or to_table not in columns:
        raise ValueError("explicit foreign key references an unknown table")
    if not set(map(str, from_cols)) <= columns[from_table] or not set(map(str, to_cols)) <= columns[to_table]:
        raise ValueError("explicit foreign key references an unknown column")
    confidence = raw.get("conf", raw.get("confidence", 1.0))
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not isfinite(float(confidence)) or not 0.0 <= confidence <= 1.0):
        raise ValueError("explicit foreign-key confidence must be in [0,1]")
    inclusion = raw.get("inclusion", confidence)
    if (isinstance(inclusion, bool) or not isinstance(inclusion, (int, float))
            or not isfinite(float(inclusion)) or not 0.0 <= inclusion <= 1.0):
        raise ValueError("explicit foreign-key inclusion must be in [0,1]")
    edge = {
        "from_table": from_table, "from_cols": tuple(map(str, from_cols)),
        "to_table": to_table, "to_cols": tuple(map(str, to_cols)),
        "conf": float(confidence), "inclusion": float(inclusion),
        "explicit": True,
    }
    if len(from_cols) == 1:
        edge.update(from_col=str(from_cols[0]), to_col=str(to_cols[0]))
    if "cardinality" in raw:
        edge["cardinality"] = str(getattr(raw["cardinality"], "value", raw["cardinality"]))
    return edge


def _fk_signature(edge):
    from_cols = tuple(edge["from_cols"]) if "from_cols" in edge else (edge["from_col"],)
    to_cols = tuple(edge["to_cols"]) if "to_cols" in edge else (edge["to_col"],)
    return edge["from_table"], from_cols, edge["to_table"], to_cols


def relate(tables, explicit_fks=()):
    """Preserve source rows and merge validated trusted edges with discovered scalar FKs."""
    explicit = [_explicit_fk(edge, tables) for edge in explicit_fks]
    merged = list(explicit)
    signatures = {_fk_signature(edge) for edge in explicit}
    # How strongly each declared local key's name names the parent it is declared to reference.
    declared_sources = {}
    entities = _entity_words(tables)
    for edge in explicit:
        for column, target in zip(edge["from_cols"], edge["to_cols"]):
            key = (edge["from_table"], column)
            declared_sources[key] = max(declared_sources.get(key, 0.0),
                                        _name_boost(column, edge["to_table"], target, entities))
    for edge in discover_fks(tables):
        # A sampled inclusion may suggest another parent for the same local
        # key. It cannot override a trusted declaration (including a component
        # of a composite key) or replace the encoder unit's declared referent,
        # unless the declaration's name names its parent less than the key names
        # the discovered one: Spider musical declares actor.Musical_ID -> actor.Actor_ID
        # (held-out 324, 2026-10-17), and the key's own word names musical.Musical_ID.
        signature = _fk_signature(edge)
        reverse = signature[2], signature[3], signature[0], signature[1]
        source = (edge["from_table"], edge["from_col"])
        overridden = (source in declared_sources and
                      _name_boost(edge["from_col"], edge["to_table"], edge["to_col"], entities)
                      <= declared_sources[source])
        if not overridden and signature not in signatures and reverse not in signatures:
            merged.append(edge)
            signatures.add(signature)
    return {"tables": tables, "fks": merged}
