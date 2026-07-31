"""Compose's multi-table JOIN base: choose the FACT table and its flatten-safe joins from the discovered
foreign keys.

FK DISCOVERY itself is NOT reimplemented here — it is owned by `engine.relations.discover_fks`, the one
deterministic inclusion-dependency detector (a child column references a parent when the parent column is a
UNIQUE key and the child's values are included in it, boosted by name/type agreement). This module adapts
that edge list to the (child, child_col, parent, parent_col) tuples `join_plan` needs and builds the
flattened SQLite view.

Keeping ONE detector means the compose panel and the AST planner agree on every join — including STRING
foreign keys (orders.customer -> customers.name), whose key is a name, not a number. A foreign key is a
referential inclusion, not a numeric type, so the readable-name join is a first-class relationship, not a
special case.
"""
from __future__ import annotations

from engine.relations import discover_fks as _discover_fk_edges


def discover_fks(tables):
    """-> [(child_table, child_col, parent_table, parent_col)] many-to-one, one FK per (child, col).

    Thin adapter over the shared inclusion-dependency detector (`engine.relations.discover_fks`), which
    already returns the single best parent per child column with a name/type-boosted confidence and a strict
    unique-key requirement on the parent (so a fanned-out or coincidental match is not emitted)."""
    return [(f["from_table"], f["from_col"], f["to_table"], f["to_col"]) for f in _discover_fk_edges(tables)]


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
