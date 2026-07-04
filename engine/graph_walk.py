"""Unit-graph builder for the column router: consumes a pre-built UNIT list and produces the parse-tree
edges + per-unit target/mask. Edges: SAME_CELL links a cell's head to its content-noun units; SAME_COL links
a column name to its cells + values down a column (where a name AGGREGATES its values); SAME_ROW links
values across a row. Pure structure — no NLP models run here.
"""
from __future__ import annotations
import numpy as np

E_NONE, E_COL, E_ROW, E_SELF, E_CELL = 0, 1, 2, 3, 4
N_EDGE = 5


def fam_dims_map(alloc):
    m = {}
    for d in alloc["dims"]:
        m.setdefault(d["family"], []).append(d["name"])
    return m


def iter_unit_texts(table):
    for u in table["units"]:
        yield u["text"]


def edges_from_meta(meta):
    """Parse-tree edges from per-unit (kind, col, row). Shared by training and serving."""
    S = len(meta)
    E = np.zeros((S, S), np.int64)
    for i in range(S):
        ki, ci, ri = meta[i]
        for j in range(S):
            if i == j:
                E[i, j] = E_SELF; continue
            kj, cj, rj = meta[j]
            if ci == cj and ri == rj and ri >= 0:
                E[i, j] = E_CELL                                   # head <-> its content-noun units
            elif ci == cj:
                E[i, j] = E_COL                                    # column name <-> its values, values down a col
            elif ki == "value" and kj == "value" and ri == rj:
                E[i, j] = E_ROW
    return E


def build_from_units(table, aid, fam_dims, nc, max_units=320):
    units = table["units"][:max_units]
    S = len(units)
    if S < 2:
        return None
    texts = [u["text"] for u in units]
    meta = [(u["kind"], u["col"], u["row"]) for u in units]
    E = edges_from_meta(meta)
    ct = np.zeros((S, nc), np.float32); cm = np.zeros((S, nc), bool)
    for ui, u in enumerate(units):
        fired = set(u["fired"])
        for fam in u["sup"]:
            for dn in fam_dims.get(fam, []):
                d = aid.get(dn)
                if d is not None:
                    cm[ui, d] = True
                    if dn in fired:
                        ct[ui, d] = 1.0
    is_val = np.array([1 if m[0] == "value" else 0 for m in meta], np.int64)
    return {"texts": texts, "meta": meta, "E": E, "ct": ct, "cm": cm, "is_val": is_val, "S": S}
