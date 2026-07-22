"""The CONNECTED / UNCONNECTED bridge design. Each uploaded string column is split:

  "<csv> connected to wikipedia"   : PK + FK columns to world tables. A column is CONNECTED when >=80% of its
                                     VALUES resolve to a world type (value-membership routing). The FK is the
                                     resolved key (city -> settlement qid -> country), so a filter that is
                                     NOT in the upload ("France") is answered by joining the world.
  "<csv> unconnected to wikipedia" : PK + a unified-encoder VECTOR per free-text column (e.g. remarks). Free
                                     text doesn't resolve to any world entity, so we keep its MEANING as an
                                     embedding.

This enables HYBRID structured + semantic SQL in one query:
  "who complained about bad delivery in France"
     = connected:   city resolves -> country = 'France'        (structured world join)
     + unconnected: remarks  <=>  embed('bad delivery')         (semantic similarity)
The unconnected vector and the prompt predicate MUST come from the SAME unified encoder, else `<=>` is
cross-space garbage — which is exactly why the encoder is unified.

This module is a SELF-CONTAINED LOCAL demonstration (SQLite + numpy cosine) of the hybrid query shape.
PRODUCTION wires the same shape onto the live world Postgres + pgvector (`<=>` operator, the bge/altLabel
resolver for the connected FKs) — see engine.knowledge_query. The serving path imports STOP (the shared
predicate stopword list) from here.
"""
from __future__ import annotations
import numpy as np

STOP = {"who", "what", "which", "show", "list", "find", "get", "the", "a", "an", "in", "of", "for", "with",
        "about", "did", "do", "we", "is", "are", "to", "complained", "complain", "had", "have", "that", "and",
        "from", "me", "all", "on", "by"}


def _norm(v):
    n = np.linalg.norm(v)
    return v / n if n > 1e-9 else v


class Bridge:
    """connected/unconnected split + the hybrid query, with the resolver + encoder injected (so this is testable
    locally with a small map, and the SAME logic drives the production Postgres/pgvector path)."""

    VALUE_ROUTE_MIN = 0.80

    def __init__(self, encode, resolve):
        self.encode = encode      # texts -> (n, d) unified-encoder vectors (unconnected cols + the prompt predicate)
        self.resolve = resolve    # cell value -> {"type","key","country"} | None  (the world resolver)

    # ---------- split + build ----------
    def split(self, table):
        """Return (connected_cols, unconnected_cols). A string column is CONNECTED if >=80% of its non-null values
        resolve to a world type; otherwise it's free text -> UNCONNECTED. Numeric columns stay on the base table."""
        cols = table["columns"]; rows = table["rows"]
        connected, unconnected = [], []
        for ci, c in enumerate(cols):
            vals = [r[ci] for r in rows if r[ci] is not None and str(r[ci]).strip()]
            if not vals or all(_is_num(v) for v in vals):
                continue                                     # numeric / empty -> not a bridge column
            hits = sum(1 for v in vals if self.resolve(str(v)))
            (connected if hits / len(vals) >= self.VALUE_ROUTE_MIN else unconnected).append((ci, c))
        return connected, unconnected

    def build(self, table):
        """Materialize the two logical bridge tables for an upload."""
        cols = table["columns"]; rows = table["rows"]
        connected, unconnected = self.split(table)
        conn_rows = []
        for pk, r in enumerate(rows):
            fks = {}
            for ci, c in connected:
                res = self.resolve(str(r[ci])) if r[ci] is not None else None
                fks[c] = res                                 # {"type","key","country"} | None
            conn_rows.append({"pk": pk, "fks": fks})
        unconn = {}
        for ci, c in unconnected:
            texts = [("" if r[ci] is None else str(r[ci])) for r in rows]
            vecs = self.encode(texts)
            unconn[c] = np.stack([_norm(np.asarray(v, np.float32)) for v in vecs])
        return {"connected": {"cols": [c for _, c in connected], "rows": conn_rows},
                "unconnected": {"cols": [c for _, c in unconnected], "vecs": unconn},
                "n": len(rows)}

    # ---------- the hybrid query ----------
    def hybrid(self, table, bridge, question, country=None, top_k=None):
        """world filter (country, via the connected FKs) + a free-text SEMANTIC predicate (vs the unconnected
        vectors). Returns rows ranked by semantic score, restricted to the world filter."""
        import re
        low = question.lower()
        # 1) the structured world filter: a country named in the question (resolved, NOT in the upload).
        if country is None:
            for w in re.findall(r"[a-z]+", low):
                res = self.resolve(w)
                if res and res.get("type") == "country":
                    country = res["key"]; break
        # 2) the residual free-text predicate -> embed -> cosine vs each unconnected column.
        pred = " ".join(w for w in re.findall(r"[a-z]+", low)
                        if w not in STOP and not (self.resolve(w) or {}).get("type"))
        pv = _norm(np.asarray(self.encode([pred])[0], np.float32)) if pred else None
        ucols = bridge["unconnected"]["vecs"]
        out = []
        for pk in range(bridge["n"]):
            # world filter: every connected col that resolves must match the country (if a country was asked)
            if country is not None:
                ok = False
                for c in bridge["connected"]["cols"]:
                    res = bridge["connected"]["rows"][pk]["fks"].get(c)
                    if res and res.get("country") == country:
                        ok = True
                if not ok:
                    continue
            sem = 0.0
            if pv is not None and ucols:
                sem = max(float(pv @ ucols[c][pk]) for c in ucols)
            out.append((pk, sem))
        out.sort(key=lambda z: -z[1])
        if top_k:
            out = out[:top_k]
        return {"country": country, "predicate": pred, "rows": out}


def _is_num(v):
    try:
        float(str(v).replace(",", "").lstrip("$").rstrip("%")); return True
    except ValueError:
        return False
