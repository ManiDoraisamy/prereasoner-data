"""Deterministic many-to-one FK discovery for the multi-table JOIN base of the composition engine (the
offline analog of engine.relations / the live world resolution). A child.col is an FK to parent.key when
parent.key is UNIQUE and every child value is contained in it (an inclusion dependency), with a name/shape
compatibility check. The live serving path (engine.world_compose) uses the world resolution instead; this
keeps the composition engine testable in plain SQLite.
"""
from __future__ import annotations
import re


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
                    name_ok = cc.lower() == pc.lower() or pc.lower() in cc.lower()
                    if not name_ok and cc.lower().endswith(("id", "_id")):
                        # a generic id suffix alone is NOT evidence: small integer id ranges make
                        # concert_ID ⊆ Stadium_ID "inclusion-true" while being no FK. Require the id
                        # column's STEM to name the parent (Owner_ID names 'owners'; concert_ID does
                        # not name 'stadium'); a bare 'Id' child column may key an id-named parent col.
                        stem = re.sub(r"_?ids?$", "", cc.lower())
                        name_ok = ((stem in parent["name"].lower() or stem in pc.lower()) if stem
                                   else pc.lower().endswith("id"))
                    if cvals <= pset and name_ok:                    # inclusion dependency + name/shape compatible
                        out.append((child["name"], cc, parent["name"], pc)); seen.add((child["name"], cc))
                        break
                if (child["name"], cc) in seen:
                    break
    return out


def join_plan(tables, fks):
    """pick the FACT (the child referencing the most parents) and its joins. -> (fact_name, [(dim, fk, pk, keep)])
    where keep = the dimension's descriptive columns (everything but the join key). None if there is no FK.

    Two flatten-safety rules (the flattened base is ONE relation, so its column names must be unique and
    every parent may appear only once in FROM):
      * one join per parent table — id-range inclusions can discover TWO fks from the fact to the same
        parent; joining it twice makes every parent column reference ambiguous. The best-NAMED fk wins.
      * `keep` drops columns whose name is already taken by the fact or an earlier dim (singer.Name +
        stadium.Name), which would otherwise collide in the view's output row."""
    if not fks:
        return None
    by_name = {t["name"]: t for t in tables}
    children = [f[0] for f in fks]
    fact = max(set(children), key=children.count)

    def name_score(cc, pc):
        return 2 if cc.lower() == pc.lower() else (1 if pc.lower() in cc.lower() else 0)

    best = {}                                                # parent -> (fk_col, pk_col), best-named fk
    for ch, cc, pa, pc in fks:
        if ch != fact:
            continue
        if pa not in best or name_score(cc, pc) > name_score(*best[pa]):
            best[pa] = (cc, pc)
    joins, used = [], {c.lower() for c in by_name[fact]["columns"]}
    for pa, (cc, pc) in best.items():
        keep = []
        for c in by_name[pa]["columns"]:
            if c != pc and c.lower() not in used:            # fact wins a name clash; then first dim wins
                keep.append(c); used.add(c.lower())
        joins.append((pa, cc, pc, keep))
    return fact, joins
