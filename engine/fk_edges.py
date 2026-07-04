"""Relational-attention edge types for the multi-table graph: the single-table parse-tree edges plus a
cross-table `fk` edge — a foreign-key column's units linked to the referenced primary-key column's units
ACROSS tables.

A unit may carry `ref=(ref_table, ref_col)` (set on a FK column from the deterministic FK discovery in
engine.relations); `edges` wires E_FK between that column's units and the referenced PK column's units.
Within a table the parse-tree edges apply (same_col/same_row/same_cell); question/SQL edges link the query
tokens to the schema.
"""
from __future__ import annotations
import numpy as np

E_NONE, E_COL, E_ROW, E_SELF, E_CELL, E_QSEQ, E_QLINK, E_SQLSEQ, E_CLAUSE, E_FK = range(10)
N_EDGE = 10


def fam_dims_map(alloc):
    m = {}
    for d in alloc["dims"]:
        m.setdefault(d["family"], []).append(d["name"])
    return m


def edges(units):
    """units: dicts with group {schema,q,sql}, kind, table, col, colname, row, clause(sql), ref(=(rt,rc) on FK cols)."""
    S = len(units)
    E = np.zeros((S, S), np.int64)
    g = [u["group"] for u in units]
    k = [u.get("kind") for u in units]
    tb = [u.get("table") for u in units]
    c = [u.get("col", -1) for u in units]
    cn = [u.get("colname") for u in units]
    r = [u.get("row", -1) for u in units]
    cl = [u.get("clause") for u in units]
    ref = [u.get("ref") for u in units]
    for i in range(S):
        for j in range(S):
            if i == j:
                E[i, j] = E_SELF; continue
            if g[i] == "schema" and g[j] == "schema":
                if tb[i] == tb[j]:                                          # within a table: parse-tree edges
                    if "table" in (k[i], k[j]):                            # the table-NAME unit <-> its column NAMES:
                        if k[i] == "name" or k[j] == "name":               # reuse the TRAINED name->members edge (E_COL,
                            E[i, j] = E_COL                                # the same one linking a column name to its values)
                    elif c[i] == c[j] and r[i] == r[j] and r[i] >= 0:
                        E[i, j] = E_CELL
                    elif c[i] == c[j] and c[i] >= 0:
                        E[i, j] = E_COL
                    elif k[i] == "value" and k[j] == "value" and r[i] == r[j] and r[i] >= 0:
                        E[i, j] = E_ROW
                elif (ref[i] is not None and ref[i][0] == tb[j] and ref[i][1] == cn[j]) or \
                     (ref[j] is not None and ref[j][0] == tb[i] and ref[j][1] == cn[i]):
                    E[i, j] = E_FK                                          # FK column <-> referenced PK column (cross-table)
            elif g[i] == "sql" and g[j] == "schema" and c[i] >= 0 and c[i] == c[j] and tb[i] == tb[j]:
                E[i, j] = E_COL
            elif g[i] == "schema" and g[j] == "sql" and c[j] >= 0 and c[i] == c[j] and tb[i] == tb[j]:
                E[i, j] = E_COL
            elif g[i] == "q" and g[j] == "schema" and k[j] == "name":
                E[i, j] = E_QLINK
            elif g[i] == "schema" and g[j] == "q" and k[i] == "name":
                E[i, j] = E_QLINK
            elif g[i] == "q" and g[j] == "q":
                E[i, j] = E_QSEQ
            elif g[i] == "sql" and g[j] == "sql":
                E[i, j] = E_CLAUSE if cl[i] == cl[j] else E_SQLSEQ
    return E
