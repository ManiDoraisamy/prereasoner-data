"""Deterministic many-to-one FK discovery for the multi-table JOIN base of the composition engine (the
offline analog of engine.relations / the live world resolution). A child.col is an FK to parent.key when
parent.key is UNIQUE and every child value is contained in it (an inclusion dependency), with a name/shape
compatibility check. The live serving path (engine.world_compose) uses the world resolution instead; this
keeps the composition engine testable in plain SQLite.
"""
from __future__ import annotations


def _col_vals(t, ci):
    return [str(r[ci]) for r in t["rows"] if ci < len(r) and r[ci] is not None and str(r[ci]).strip()]


def discover_fks(tables):
    """-> [(child_table, child_col, parent_table, parent_col)] many-to-one, one FK per (child,col)."""
    out, seen = [], set()
    for child in tables:
        for ci, cc in enumerate(child["columns"]):
            cvals = set(_col_vals(child, ci))
            if not cvals or (child["name"], cc) in seen:
                continue
            for parent in tables:
                if parent["name"] == child["name"]:
                    continue
                for pi, pc in enumerate(parent["columns"]):
                    pvals = _col_vals(parent, pi)
                    pset = set(pvals)
                    if len(pset) < 2 or len(pset) != len(pvals):     # parent key must be UNIQUE (a real key)
                        continue
                    name_ok = cc.lower() == pc.lower() or pc.lower() in cc.lower() or cc.lower().endswith(("id", "_id"))
                    if cvals <= pset and name_ok:                    # inclusion dependency + name/shape compatible
                        out.append((child["name"], cc, parent["name"], pc)); seen.add((child["name"], cc))
                        break
                if (child["name"], cc) in seen:
                    break
    return out


def join_plan(tables, fks):
    """pick the FACT (the child referencing the most parents) and its joins. -> (fact_name, [(dim, fk, pk, keep)])
    where keep = the dimension's descriptive columns (everything but the join key). None if there is no FK."""
    if not fks:
        return None
    by_name = {t["name"]: t for t in tables}
    children = [f[0] for f in fks]
    fact = max(set(children), key=children.count)
    joins = []
    for ch, cc, pa, pc in fks:
        if ch != fact:
            continue
        keep = [c for c in by_name[pa]["columns"] if c != pc]
        joins.append((pa, cc, pc, keep))
    return fact, joins
