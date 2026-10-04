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
from math import isfinite
from engine.numeric import parse_decimal

# One served question derives the same tables up to ten times (the decomposition probe, compose, the
# delegate and its fallbacks): a 6-tab, 34,500-row subscriptions workbook spent 52 s of a request on FK
# discovery, and built its planner schema and schema graph four times each (2026-10-04). Each is a pure
# function of the tables' content, so within one request (`request_memo`) a repeat reuses the first result.
_MEMO = contextvars.ContextVar("request_memo", default=None)
_MISSING = object()


@contextlib.contextmanager
def request_memo():
    """Scope `memoized` reuse to one served request."""
    token = _MEMO.set({})
    try:
        yield
    finally:
        _MEMO.reset(token)


def memoized(kind, key, compute):
    """``compute()``, or within `request_memo` the result an equal ``key()`` of the same kind already
    computed. A key that cannot be hashed (an unhashable cell) computes afresh."""
    memo = _MEMO.get()
    if memo is None:
        return compute()
    try:
        slot = (kind, key())
        found = memo.get(slot, _MISSING)
    except TypeError:
        return compute()
    if found is _MISSING:
        found = memo[slot] = compute()
    return found


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


def _norm(v):
    return None if v in (None, "") else str(v).strip().lower()


def is_key(values):
    # A join/FK TARGET must be EXACTLY unique — a 0.98 tolerance let a column with a few duplicate keys become
    # an FK target, so a fact row matched multiple parent rows and the join fanned out (inflating SUM/COUNT/AVG).
    # This strict uniqueness rule is the one both paths use: engine.joins.discover_fks (the compose/SQLite
    # path) delegates here, so the planner and the compose panel share this exact key test.
    nn = [_norm(v) for v in values if _norm(v) is not None]
    return len(nn) >= 2 and len(nn) == len(values) and len(set(nn)) == len(nn)   # unique (exact), no nulls


def _name_boost(ax, bname, by):
    axl, bl, byl = ax.lower(), bname.lower(), by.lower()
    root = (bl[:-3] + "y") if bl.endswith("ies") else bl.rstrip("s")    # cities->city, orders->order
    pre = axl[:-3] if axl.endswith("_id") else (axl[:-2] if axl.endswith("id") else axl)
    b = 0.0
    if axl.endswith("id"):
        b += 0.15
    if pre and (pre == root or pre == bl or (len(pre) >= 3 and (root.startswith(pre) or pre.startswith(root)))):
        b += 0.35
    if axl == byl:
        b += 0.10
    return b


def layout(columns):
    """A table's layout: its column names, ignoring case, spacing and order. Tables with one layout are
    copies of one export kept per product, month or keyword list."""
    return frozenset(" ".join(str(column).lower().split()) for column in columns)


def discover_fks(tables, min_incl=0.9):
    """The tables' many-to-one foreign keys (the module's inclusion-dependency rule)."""
    found = memoized("fks", lambda: (min_incl, content_key(tables)), lambda: _discover_fks(tables, min_incl))
    return [dict(fk) for fk in found]


def _discover_fks(tables, min_incl):
    keys = {}                                                  # (table, col) -> set of normalized key values
    for t in tables:
        for ci, c in enumerate(t["columns"]):
            vals = cells(t, ci)
            if is_key(vals):
                keys[(t["name"], c)] = {_norm(v) for v in vals if _norm(v) is not None}
    types = {}                                                 # a column's coltype, read at most once

    def type_of(table, index):
        if (id(table), index) not in types:
            types[(id(table), index)] = coltype(cells(table, index))
        return types[(id(table), index)]

    fks = []
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
                nb = _name_boost(ax, bname, by)
                if nb > 0.0:
                    candidates.append((bname, by, bset, B, nb))
            if not candidates:
                continue
            avals = [_norm(v) for v in cells(A, axi) if _norm(v) is not None]
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
    if from_table not in columns or to_table not in columns or from_table == to_table:
        raise ValueError("explicit foreign key references an unknown or identical table")
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
    for edge in discover_fks(tables):
        if _fk_signature(edge) not in signatures:
            merged.append(edge)
            signatures.add(_fk_signature(edge))
    return {"tables": tables, "fks": merged}
