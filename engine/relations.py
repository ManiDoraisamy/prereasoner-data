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


def _num(v):
    try:
        float(str(v).strip().replace(",", "").lstrip("$").rstrip("%")); return True
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


def dedup(t):
    """Drop exact duplicate rows (case/space-insensitive). Mutates + returns the table."""
    seen, out, dropped = set(), [], 0
    for row in t["rows"]:
        k = tuple(_norm(v) for v in row)
        if k in seen:
            dropped += 1; continue
        seen.add(k); out.append(row)
    t["rows"] = out; t["_dedup_dropped"] = dropped
    return t


def is_key(values):
    # A join/FK TARGET must be EXACTLY unique — a 0.98 tolerance let a column with a few duplicate keys become
    # an FK target, so a fact row matched multiple parent rows and the join fanned out (inflating SUM/COUNT/AVG).
    # This mirrors the strict uniqueness check in joins.discover_fks (the compose/SQLite path).
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


def discover_fks(tables, min_incl=0.9):
    keys = {}                                                  # (table, col) -> set of normalized key values
    for t in tables:
        for ci, c in enumerate(t["columns"]):
            vals = cells(t, ci)
            if is_key(vals):
                keys[(t["name"], c)] = {_norm(v) for v in vals if _norm(v) is not None}
    fks = []
    for A in tables:
        for axi, ax in enumerate(A["columns"]):
            avals = [_norm(v) for v in cells(A, axi) if _norm(v) is not None]
            if not avals:
                continue
            aset, at = set(avals), coltype(cells(A, axi))
            many_to_one = len(aset) < len(avals)               # the FK column REPEATS (it is not itself unique)
            best = None
            for (bname, by), bset in keys.items():
                if bname == A["name"]:
                    continue                                   # a join FK lives ACROSS tables, never same-table
                B = next(t for t in tables if t["name"] == bname)
                if at != coltype(cells(B, B["columns"].index(by))):
                    continue
                if len(aset) > len(bset):
                    continue                                   # FK side has <= the key's distinct cardinality
                incl = len(aset & bset) / len(aset)
                if incl < min_incl:
                    continue
                nb = _name_boost(ax, bname, by)
                if not many_to_one and nb < 0.3:               # a unique/1:1 column is a FK only WITH a name signal —
                    continue                                   # else two independent keys with overlapping ranges fake one
                conf = min(1.0, incl * 0.6 + nb + (0.2 if many_to_one else 0.0))
                if best is None or conf > best[2]:
                    best = (bname, by, conf, incl)
            if best and best[2] >= 0.6:
                fks.append({"from_table": A["name"], "from_col": ax, "to_table": best[0], "to_col": best[1],
                            "conf": round(best[2], 2), "inclusion": round(best[3], 2)})
    return fks


def relate(tables):
    """Dedup every table, then discover the FK graph. -> {tables, fks}."""
    for t in tables:
        dedup(t)
    return {"tables": tables, "fks": discover_fks(tables)}
