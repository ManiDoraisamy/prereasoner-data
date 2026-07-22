"""KnowledgeQuery — the UNIFIED ENCODER wired into the LIVE /api/knowledge path, end to end.

This realizes the unified-encoder objective in production (not just /api/dimension analyze):
  * OPERATOR + OPERAND from the metric space  — inherited EncoderQuery.read_op_all (no MEASURE_NOUNS/table_noun);
    the delegate (aggregate / pure world-join) path is EntityQuery.serve, which calls THIS read_op_all via MRO.
  * BRIDGE TABLES persisted per user on Postgres (the thesis: "an interpretable model is a database"):
      "<t> connected to wikipedia"   = resolved FKs (cell -> world key + country), via bge + world.words (exact
                                       entity resolution; same-space NOT required — the join is on a string key).
      "<t> unconnected to wikipedia" = a unified-encoder vector(896) per free-text cell (remarks, notes, …),
                                       so a free-text MEANING is kept as an embedding.
  * HYBRID structured+semantic query — "who complained about bad delivery in France" =
        connected:   country = 'France'                      (world join, bge-resolved)
      + unconnected: remarks <=> embed('bad delivery')       (pgvector cosine, UNIFIED encoder both sides)
    The predicate vector and the stored column vectors come from the SAME unified encoder (EncoderQuery._encode),
    so `<=>` is a valid same-space cosine — the reason the encoder had to be unified first.

Class graph:  KnowledgeQuery(EncoderQuery, EntityQuery)
  MRO = [KnowledgeQuery, EncoderQuery, EntityQuery, RoutedQuery, PgQuery, KnowledgeTableQuery, TableQuery, …] so:
    - read_op_all / read_op_model / _is_id  resolve to EncoderQuery (the metric-space operator), NOT keywords.
    - serve / meaning_filter / _world_joins / route / _resolve  resolve to EntityQuery (the world machinery + bge).
    - schema / _encode / _layers  resolve to TableQuery, but run on the UNIFIED qwen (overlaid in __init__).
"""
from __future__ import annotations
import os

import numpy as np

from engine.config import DATA_DIR, kb_model_route_enabled
from engine.tables import qident, qlit
from engine.knowledge_tables import KnowledgeTableQuery
from engine.pg import _pg, _PGTYPE
from engine.entities import EntityQuery, WORLD_TABLE_TYPE, TYPE_TO_FRIENDLY
from engine.embeddings import Embedder, pgvector_literal, normalize_surface
from engine.encoder_overlay import EncoderQuery, load_encoder
from engine.bridge import STOP


def _norm_vec(v):
    v = np.asarray(v, np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 1e-9 else v


def _cos(a, b):
    a = np.asarray(a, np.float32); b = np.asarray(b, np.float32)
    return float(a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9))


def _is_num(v):
    try:
        float(str(v).replace(",", "").lstrip("$").rstrip("%")); return True
    except (ValueError, TypeError):
        return False


class KnowledgeQuery(EncoderQuery, EntityQuery):
    """Live /api/knowledge served by the unified encoder: bge for connected entity resolution, the unified encoder
    for the anchored readout + operator + the unconnected free-text bridge. Persists the two bridge tables per
    user and answers the hybrid structured+semantic query; delegates aggregates / plain world joins to
    EntityQuery."""

    FREETEXT_MIN_AVGLEN = 12       # a non-connected text column is "free text" (embed it) if avg cell length > this
    HYBRID_LIMIT = 10

    _SHARE = ("alloc", "nc", "dims", "sid", "thr", "model", "nL", "tok", "qwen", "hdim")

    def __init__(self, deploy_dir=DATA_DIR):
        EntityQuery.__init__(self, deploy_dir)       # bge + Postgres + world metadata + spaCy
        load_encoder(self, deploy_dir)               # ONE MODEL: the trained encoder (operator+bridge+typing)
        # The planner composes a TableQuery (self.q11) for the single-table delegate path. Point it at the SAME
        # encoder (shared refs — ONE Qwen in memory) so EVERY path goes through the one trained model.
        if getattr(self, "q11", None) is not None:
            for a in self._SHARE:
                setattr(self.q11, a, getattr(self, a))

    # ---------------- MODEL-DRIVEN world-table routing ----------------
    # router leaf -> the world `type` (and thence friendly table + TYPE_QID). The TRAINED model types the
    # column; value-membership routing is kept only as a coverage fallback for columns the model leaves untyped.
    _LEAF_WTYPE = {"city": "city", "country": "country", "u_s_state": "state"}

    GROUND_FRAC = 0.8            # a model-typed column joins only if >=80% of cells GROUND. The trained model
                                 # proposes the TYPE but CANNOT reliably separate a city column from a NAME column
                                 # (it fires the city dim even HIGHER for short first-names — Ada/Bo/Sam are real cities),
                                 # so grounding is the decisive gate here: a real city column grounds ~100%, a name
                                 # column grounds ~50-75%. 0.8 (the value-membership bar) cleanly separates them. The
                                 # model still genuinely drives country/u_s_state typing (clean model separation).

    def _router(self):
        """The typing router REUSES the already-loaded encoder+readout (self.qwen/tok/model) — NOT a second
        model. So 'everything goes through the one model' literally: one Qwen, used for operator, bridge, AND typing."""
        r = getattr(self, "_column_router", None)
        if r is None:
            from engine.router import Router
            r = self._column_router = Router(shared=(self.qwen, self.tok, self.model))
        return r

    def _grounds(self, cells, wtype):
        """does the column GROUND? i.e. do enough of its cells resolve to real `wtype` entities? Cheap exact-
        normalized membership over knowledgebase."words" (the bridge does the fuzzy remainder later). The MODEL decides
        the TYPE; this only confirms the cells belong to the world — so a loose city false-positive on a name
        column (cells don't ground) is dropped (null), not joined. Returns True iff grounded."""
        norms = sorted({normalize_surface(str(c)) for c in cells if str(c).strip()})
        if len(norms) < 2:
            return False
        cur = self._rconn().cursor()
        cur.execute('SELECT COUNT(DISTINCT norm) FROM knowledgebase."words" WHERE type=%s AND norm = ANY(%s)', (wtype, norms))
        hit = cur.fetchone()[0]
        return hit >= max(2, self.GROUND_FRAC * len(norms))

    def route(self, table):
        """MODEL-DRIVEN routing — the TRAINED model types each string column to its world table, and THAT drives
        the world join + the world_qid FK (NOT value-membership). The model decides the TYPE; a typed column
        only JOINS if its cells GROUND to that type (_grounds) — so a loose false-positive (name->city) is
        dropped because the names don't resolve, never a wrong answer. super()'s value-membership routing fills
        ONLY columns the model leaves untyped (coverage). Any model failure (or KB_MODEL_ROUTE=0) falls back
        to pure value-membership so the live demo can never hard-break."""
        import hashlib
        sig = (table["name"], tuple(table["columns"]),
               hashlib.sha1(repr([tuple(r) for r in table["rows"]]).encode("utf-8", "replace")).hexdigest()[:12])
        cache = self.__dict__.setdefault("_route_cache", {})               # per-table routing cache: the router runs ONCE
        if sig in cache:                                                   # per (schema,values), not on every query
            return dict(cache[sig])
        routes = {}
        if kb_model_route_enabled():
            try:
                r = self._router()
                for ci, col in enumerate(table["columns"]):
                    cells = [str(rw[ci]) for rw in table["rows"] if ci < len(rw) and rw[ci] not in (None, "")]
                    if len(cells) < 3:
                        continue
                    if self._avglen(table, col) > self.FREETEXT_MIN_AVGLEN:  # free-text (remarks/notes) is NEVER a world
                        continue                                            # entity + slow to encode -> skip (perf + sense)
                    o = r.route(cells, header=col, world_only=True)        # model types -> {city,country,u_s_state} or None
                    if not (o and o["leaf"] in self._LEAF_WTYPE):
                        continue
                    wtype = self._LEAF_WTYPE[o["leaf"]]
                    friendly = TYPE_TO_FRIENDLY.get(wtype)
                    if friendly in self.words and self._grounds(cells, wtype):  # MODEL types + cells GROUND
                        routes[(table["name"], col)] = friendly
            except Exception as e:                                         # NEVER silent: log loudly so a degradation
                import traceback                                           # to value-membership is visible in the logs
                print(f"[knowledge_query] !! MODEL ROUTING FAILED -> value-membership fallback: {e!r}", flush=True)
                traceback.print_exc()
                routes = {}
        for k, v in super().route(table).items():
            routes.setdefault(k, v)                                        # coverage fallback (never override the model)
        if len(cache) > 100:
            cache.clear()
        cache[sig] = dict(routes)
        return routes

    # ---------------- NON-GEO world join + LAZY fill ----------------
    # The faithful Wikidata tables (knowledgebase."hospital"/"software"/...) join like the geo ones: resolve the uploaded
    # cell -> the type's qid (world.words, type=<leaf>), JOIN knowledgebase."<leaf>" ON qid, filter by a world attribute
    # (country), aggregate the uploaded metric. Cells not in words are LAZY-filled from Wikidata first.
    def _resolve_world_qid(self, value, label, type_qid):
        """value -> the world qid. world.words.type stores the EXACT Wikidata label (what knowledge_sync's
        ensure_entity inserts), NOT the snake routing leaf — so look it up by that exact label, else a lazy-filled
        multi-word type ('academic journal' vs the routing 'academic_journal') misses the fast path forever. Exact
        norm match, then bge NN, then LAZY Wikidata fill on a miss."""
        cur = self._rconn().cursor()
        cur.execute('SELECT label FROM knowledgebase."types" WHERE qid=%s', (type_qid,))
        _r = cur.fetchone()
        wl = (str(_r[0]) if _r and _r[0] else label)                  # the exact label, matching the lazy insert
        n = normalize_surface(value)
        cur.execute('SELECT qid FROM knowledgebase."words" WHERE type=%s AND norm=%s AND qid IS NOT NULL LIMIT 1', (wl, n))
        row = cur.fetchone()
        if row:
            return row[0]
        vec = pgvector_literal(Embedder.get().encode([value])[0])
        cur.execute('SELECT qid, 1-(embedding <=> %s::vector) FROM knowledgebase."words" WHERE type=%s AND qid IS NOT NULL '
                    'ORDER BY embedding <=> %s::vector LIMIT 1', (vec, wl, vec))
        row = cur.fetchone()
        if row and row[1] is not None and row[1] >= 0.85:
            return row[0]
        try:                                                              # LAZY: pull this one entity from Wikidata
            from engine.knowledge_sync import lazy_resolve
            return lazy_resolve(value, type_qid, label)
        except Exception as e:                                            # noqa: BLE001
            print(f"[knowledge_query] lazy_resolve failed for {value!r}/{label}: {e}", flush=True); return None

    def _world_type_map(self):
        """{snake(leaf label) -> type qid} for the mirrored non-geo tables, from taxonomy.csv. Cached."""
        m = getattr(self, "_wtmap", None)
        if m is None:
            import csv as _csv
            from engine.knowledge_sync import snake
            m = {}
            try:
                for r in _csv.DictReader(open(DATA_DIR / "taxonomy.csv", encoding="utf-8")):
                    if r.get("status") in ("accepted", "added"):
                        cats = [r[f"category_{i}"] for i in range(1, 10) if r.get(f"category_{i}")]
                        if cats:
                            m[snake(cats[-1])] = r["qid"]
            except Exception:                                            # noqa: BLE001
                pass
            self._wtmap = m
        return m

    def _nongeo_plan(self, norm, question):
        """If a string column types (THROUGH the trained model) to a non-geo leaf that HAS a faithful world table
        + a country filter the question names + the question naming that type, return the plan. The MODEL drives
        it: Router.route non-geo leaves by their DIRECT leaf-dim firing (the path-decay router mis-picks because
        broad ancestors like 'organization' are shared — a hospital column fires the hospital dim 0.197 >
        street/sports_team); Wikidata selects by the DIRECT leaf dim. No column-header heuristic."""
        if not kb_model_route_enabled():
            return None
        import re as _re
        cr = self._resolve(question, "country")
        if not cr:                                                        # only the country-filtered non-geo agg for now
            return None
        ql = question.lower()
        tmap = self._world_type_map()
        try:
            r = self._router()
        except Exception:                                                # noqa: BLE001
            return None
        cur = self._rconn().cursor()
        for t in norm:
            for ci, col in enumerate(t["columns"]):
                cells = [str(rw[ci]) for rw in t["rows"] if ci < len(rw) and rw[ci] not in (None, "")]
                if len(cells) < 3:                                        # entity names are long but model+grounding gate
                    continue
                o = r.route(cells, header=col, world_only=False, min_fire=0.12)   # the MODEL types the column
                for lf in ([o["leaf"]] if o else []):
                    if lf in self._LEAF_WTYPE or lf not in tmap:         # geo handled by the planner; need a faithful table
                        continue
                    # the QUESTION must name this type ('...for hospitals...') — 'total amount in France' names no type,
                    # so a person-name column (which grounds as 'writer') can't hijack the plain geo aggregate.
                    if not any(_re.search(r"\b" + w + r"s?\b", ql) for w in lf.split("_") if len(w) > 3):
                        continue
                    cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='knowledgebase' "
                                "AND table_name=%s AND column_name='country'", (lf,))
                    if not cur.fetchone():
                        continue                                          # need knowledgebase."<lf>".country to filter
                    return {"table": t, "col": col, "label": lf, "qid": tmap[lf], "country": cr[0], "cells": cells}
        return None

    def _serve_world_type(self, norm, question, sch, plan, schema):
        """Aggregate an uploaded NON-GEO table joined to its faithful Wikidata world table, filtered by country.
        e.g. hospitals.csv(hospital, beds) + 'total beds for hospitals in United States' -> resolve each hospital to
        knowledgebase.\"hospital\" (lazy-fill from Wikidata on miss), keep those whose .country = 'United States', SUM(beds)."""
        t = plan["table"]; col = plan["label"]; label = plan["label"]; country = plan["country"]
        ci = t["columns"].index(plan["col"])
        op = (self.read_op_model([t], question)[0]) or "COUNT"
        measure = None
        if op in ("SUM", "AVG"):
            measure = next((c["name"] for c in sch if c["table"] == t["name"]
                            and c.get("affinity") in ("INTEGER", "REAL") and not self._is_id(c["name"])), None)
            if not measure:
                op = "COUNT"
        mi = t["columns"].index(measure) if measure else None

        pairs = []                                                        # (world_qid, measure_value)
        for rw in t["rows"]:
            v = str(rw[ci]) if ci < len(rw) and rw[ci] not in (None, "") else None
            if not v:
                continue
            wq = self._resolve_world_qid(v, label, plan["qid"])
            if not wq:
                continue
            mv = None
            if mi is not None and mi < len(rw):
                try:
                    mv = float(str(rw[mi]).replace(",", "").lstrip("$").rstrip("%"))
                except (ValueError, TypeError):
                    mv = None
            pairs.append((wq, mv))

        cur = self._rconn().cursor()
        cur.execute('SELECT label FROM knowledgebase."types" WHERE qid=%s', (plan["qid"],))
        _r = cur.fetchone(); wl = (str(_r[0]) if _r and _r[0] else label)[:63]   # wikipedia table = the EXACT Wikidata label
        qids = sorted({wq for wq, _ in pairs})
        try:                                                              # LAZY-SYNC the resolved entities into knowledgebase."<wl>"
            from engine.knowledge_sync import ensure_entity                   # (qid PK + country qid FK) so the join below hits
            for q in qids:
                ensure_entity(q, plan["qid"])
        except Exception as e:                                            # noqa: BLE001 — never block on a sync miss
            print(f"[knowledge_query] non-geo lazy-fill failed: {e}", flush=True)
        # country filter is qid = qid (plan["country"] is a country QID; w."country" is the country's qid FK)
        cur.execute(f'SELECT qid FROM knowledgebase."{wl}" WHERE qid = ANY(%s) AND lower("country") = lower(%s)', (qids, country))
        keep = {r[0] for r in cur.fetchall()}
        hit = [(wq, mv) for wq, mv in pairs if wq in keep]
        if op == "COUNT":
            val = len(hit)
        elif op == "SUM":
            val = sum(mv for _wq, mv in hit if mv is not None)
        else:                                                             # AVG
            mvs = [mv for _wq, mv in hit if mv is not None]
            val = round(sum(mvs) / len(mvs), 2) if mvs else 0
        val = int(val) if isinstance(val, float) and val == int(val) else val
        disp = f'{op}({measure})' if measure else 'COUNT(*)'
        sql = (f'SELECT {disp} FROM "{t["name"]}" u JOIN knowledgebase."{wl}" w ON w.qid = resolve(u."{plan["col"]}") '
               f'WHERE w."country" = {qlit(country)}')
        return {"question": question, "as_of": None, "sql": sql,
                "result": {"columns": [disp], "rows": [[val]]},          # "columns" (NOT "cols") — the client render +
                "model": f'engine - non-geo world join (knowledgebase."{wl}", lazy-filled)'}  # geo path both use .columns

    # ---------------- connected / unconnected split ----------------
    def _avglen(self, table, col):
        ci = table["columns"].index(col)
        vals = [str(r[ci]) for r in table["rows"] if r[ci] is not None and str(r[ci]).strip()]
        return (sum(len(v) for v in vals) / len(vals)) if vals else 0.0

    def _freetext_cols(self, table, connected):
        """non-connected, non-numeric columns whose cells are sentence-like (avg length > FREETEXT_MIN_AVGLEN):
        remarks/notes/comments — NOT short enums (status/tier) or names."""
        out = []
        for ci, c in enumerate(table["columns"]):
            if c in connected:
                continue
            vals = [r[ci] for r in table["rows"] if r[ci] is not None and str(r[ci]).strip()]
            if not vals or all(_is_num(v) for v in vals):
                continue
            if self._avglen(table, c) > self.FREETEXT_MIN_AVGLEN:
                out.append(c)
        return out

    def _table_plan(self, table):
        """Return {table, conn:[(col,wtype)], unconn:[col], freetext:col} for a table that has BOTH a connected
        column and a free-text column (the hybrid-eligible shape), else None."""
        routes = self.route(table)                                  # {(tname,col): friendly world table}
        conn = [(c, WORLD_TABLE_TYPE.get(routes[(table["name"], c)]))
                for c in table["columns"] if (table["name"], c) in routes]
        unconn = self._freetext_cols(table, {c for c, _ in conn})
        if not unconn:
            return None
        return {"table": table, "conn": conn, "unconn": unconn,
                "freetext": max(unconn, key=lambda c: self._avglen(table, c))}

    def _semantic_predicate(self, question, drop_surfaces=()):
        """The residual free-text predicate: question words minus stopwords minus the surface tokens of any resolved
        world entity (those drive the structured filter). 'who complained about bad delivery in France', France
        stripped -> drop who/about/in/complained (STOP) -> 'bad delivery'."""
        drop = set()
        for s in drop_surfaces:
            drop |= set(str(s).lower().split())
        words = "".join(ch.lower() if (ch.isalnum() or ch.isspace()) else " " for ch in question).split()
        return " ".join(w for w in words if w not in STOP and w not in drop).strip()

    # ---------------- the PERSISTED connected bridge ("<t> connected to wikipedia") ----------------
    # Value-keyed (one row per resolved column value). BOTH the aggregate world join (via the _city/_cell bridge
    # overrides below, which feed the inherited _world_joins) and the hybrid country filter read THIS table.
    # So there is NO inline-VALUES bridge anywhere — every world query joins a real, inspectable bridge table.
    CONN_DDL = ('("column" TEXT, "value" TEXT, "world_type" TEXT, "world_key" TEXT, "country" TEXT, "world_qid" TEXT)')
    # the FK to the qid of the WIKIDATA TABLE/type this column belongs to (world_qid); world_key stays the resolved
    # INSTANCE key (qid for city, canonical otherwise) that the join uses. The expanded world.types is keyed
    # by these same qids (Q515 city -> Cities, Q6256 country -> Countries, ...).
    TYPE_QID = {"city": "Q515", "country": "Q6256", "state": "Q35657", "continent": "Q5107", "element": "Q11344"}

    def _conn_bridge_name(self, mtab):
        return f"{mtab} connected to wikipedia"

    def _materialize(self, inner_sql):
        """Run the resolution subquery ONCE to get the resolved (cell -> world key) pairs, so we can persist
        them as a table instead of inlining them as VALUES. inner_sql already has its literals inlined (no params)."""
        cur = self._rconn().cursor()
        cur.execute(f"SELECT b.cell, b.wk FROM ({inner_sql}) AS b(cell, wk)")
        return cur.fetchall()

    def _persist_connected(self, mtab, route_col, wtype, pairs):
        """Persist resolved FKs into "<schema>"."<mtab> connected to wikipedia" (refresh this column) and return the
        SELECT the world join reads — so the inherited _world_joins references the PERSISTED table, not inline VALUES.
        `pairs`: [(cell_value, world_key)]; country is looked up from world.words by key (qid for city, canonical
        otherwise) for the hybrid country filter + interpretability."""
        schema = self._pg_schema
        bn = self._conn_bridge_name(mtab)
        cur = self._rconn().cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS {qident(schema)}')
        cur.execute(f'CREATE TABLE IF NOT EXISTS {qident(schema)}.{qident(bn)} {self.CONN_DDL}')
        # migrate old 5-col bridges -> 6 cols. ADD COLUMN takes an ACCESS EXCLUSIVE lock EVERY call even when the
        # column already exists (IF NOT EXISTS still grabs the lock to check), so guard it behind a cheap catalog
        # read: only run the migration when world_qid is genuinely absent. Steady state (column present, which the
        # CONN_DDL above already creates for fresh tables) takes NO exclusive lock -> no relation-lock contention
        # between concurrent same-sub instances.
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
                    "AND column_name='world_qid'", (schema, bn))
        if not cur.fetchone():
            cur.execute(f'ALTER TABLE {qident(schema)}.{qident(bn)} ADD COLUMN IF NOT EXISTS "world_qid" TEXT')
        cur.execute(f'DELETE FROM {qident(schema)}.{qident(bn)} WHERE "column" = %s', (route_col,))
        keys = sorted({k for _, k in pairs if k})
        country = {}
        if keys and wtype == "city":
            cur.execute('SELECT qid, canon_country FROM knowledgebase."words" WHERE type=\'city\' AND qid = ANY(%s)', (keys,))
            country = {q: cc for q, cc in cur.fetchall()}
        elif keys:
            cur.execute('SELECT canonical, canon_country FROM knowledgebase."words" WHERE type=%s AND canonical = ANY(%s)',
                        (wtype, keys))
            country = {c: (cc or c) for c, cc in cur.fetchall()}
            if wtype == "country":
                for k in keys:
                    country.setdefault(k, k)
        tqid = self.TYPE_QID.get(wtype)                                   # qid of the wikidata TABLE this column belongs to
        seen, rows = set(), []
        for cell, key in pairs:
            if key and (cell, key) not in seen:
                seen.add((cell, key)); rows.append((route_col, cell, wtype, key, country.get(key), tqid))
        if rows:
            cur.executemany(f'INSERT INTO {qident(schema)}.{qident(bn)} VALUES (%s,%s,%s,%s,%s,%s)', rows)
        self._rconn().commit()
        # Stream the cell→world lookup LIVE so the user watches "Paris → Q90 · France" resolve BEFORE the view stack
        # renders — this runs INSIDE the delegate (ahead of the engine's views). Best-effort + capped (a big upload
        # can't spam RTDB); the RTDB key can't hold . $ # [ ] / so the cell is sanitized for the key only.
        try:
            from engine.trace import ctx_emit
            _bad = str.maketrans({".": "_", "$": "_", "#": "_", "[": "_", "]": "_", "/": "_"})
            rmap, seen2 = {}, set()
            for _rc, _cell, _wt, _key, _cc, _tq in rows:
                c = str(_cell)
                if c in seen2:
                    continue
                seen2.add(c)
                rmap[c.translate(_bad)[:120] or "_"] = (f"{_key} · {_cc}" if _cc and _cc != _key else str(_key))
                if len(rmap) >= 24:
                    break
            if rmap:
                ctx_emit("resolve", rmap, merge=True)
        except Exception:                                    # noqa: BLE001 — streaming must never break the answer
            pass
        return f'SELECT "value", "world_key" FROM {qident(schema)}.{qident(bn)} WHERE "column" = {qlit(route_col)}'

    # Override the INLINE-VALUES bridges: resolve via super (same logic), persist, then point the join at
    # the persisted table. _world_joins wraps the returned SELECT as __bridge0 — now backed by a real table.
    def _city_bridge_sql(self, norm, mtab, route_col, ctx_country):
        inner = super()._city_bridge_sql(norm, mtab, route_col, ctx_country)
        return self._persist_connected(mtab, route_col, "city", self._materialize(inner)) if inner else None

    def _cell_bridge_sql(self, norm, mtab, route_col, wtype, ctx_country=None):
        inner = super()._cell_bridge_sql(norm, mtab, route_col, wtype, ctx_country)
        return self._persist_connected(mtab, route_col, wtype, self._materialize(inner)) if inner else None

    # ---------------- the unconnected bridge (free-text vectors) + main table, for the hybrid path ----------------
    def _persist_main_unconn(self, cur, schema, t, sch, plan):
        """Persist the uploaded table (with a __pk) + "<t> unconnected to wikipedia" (a unified-encoder vector per
        free-text cell, keyed by __pk). The connected bridge is persisted separately (shared with the agg path)."""
        tn = t["name"]; cols = t["columns"]; rows = t["rows"]
        affof = {c["name"]: c["affinity"] for c in sch if c["table"] == tn}
        cur.execute(f'DROP TABLE IF EXISTS {qident(schema)}.{qident(tn)} CASCADE')
        coldefs = ['"__pk" BIGINT'] + [f'{qident(c)} {_PGTYPE.get(affof.get(c, "TEXT"), "TEXT")}' for c in cols]
        cur.execute(f'CREATE TABLE {qident(schema)}.{qident(tn)} ({", ".join(coldefs)})')
        ins = f'INSERT INTO {qident(schema)}.{qident(tn)} VALUES (' + ",".join(["%s"] * (len(cols) + 1)) + ')'
        for pk, r in enumerate(rows):
            cur.execute(ins, [pk] + [KnowledgeTableQuery._coerce(r[ci], affof.get(cols[ci], "TEXT")) for ci in range(len(cols))])
        un = f"{tn} unconnected to wikipedia"
        cur.execute(f'DROP TABLE IF EXISTS {qident(schema)}.{qident(un)}')
        cur.execute(f'CREATE TABLE {qident(schema)}.{qident(un)} '
                    f'("__pk" BIGINT, "column" TEXT, "value" TEXT, "embedding" vector({self.hdim}))')
        uins = f'INSERT INTO {qident(schema)}.{qident(un)} VALUES (%s,%s,%s,%s::vector)'
        for col in plan["unconn"]:
            ci = cols.index(col)
            texts = [("" if r[ci] is None else str(r[ci])) for r in rows]
            vecs = self._encode(texts)                                # UNIFIED encoder (same space as the predicate)
            for pk, (txt, vec) in enumerate(zip(texts, vecs)):
                if txt.strip():
                    cur.execute(uins, [pk, col, txt, pgvector_literal(_norm_vec(vec))])

    # ---------------- the hybrid serve ----------------
    def _serve_hybrid(self, norm, fks, sch, question, pred, plan, country, as_of, schema):
        t = plan["table"]; tn = t["name"]
        self._pg_schema = schema                          # so the connected-bridge overrides persist to this schema
        pv = pgvector_literal(_norm_vec(self._encode([pred])[0]))     # predicate in the unified space
        cn = self._conn_bridge_name(tn); un = f"{tn} unconnected to wikipedia"
        rc = plan["conn"][0][0] if plan["conn"] else None            # the routed connected column (e.g. city)
        conn = _pg()
        try:
            cur = conn.cursor()
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS {qident(schema)}')
            cur.execute(f'SET search_path TO {qident(schema)}, knowledgebase, public')
            self._persist_main_unconn(cur, schema, t, sch, plan)
            conn.commit()
            # connected bridge = the SAME persisted table the aggregate path builds (value-keyed), via the overrides
            for col, wtype in plan["conn"]:
                if wtype == "city":
                    self._city_bridge_sql(norm, tn, col, country)
                elif wtype:
                    self._cell_bridge_sql(norm, tn, col, wtype, country)
            disp = ", ".join(f'm.{qident(c)}' for c in t["columns"])
            sql = (f'SELECT {disp} FROM {qident(schema)}.{qident(tn)} m '
                   f'JOIN {qident(schema)}.{qident(un)} u ON u."__pk" = m."__pk" AND u."column" = {qlit(plan["freetext"])} ')
            if country and rc:                              # world filter via the PERSISTED connected bridge (by value)
                sql += (f'WHERE EXISTS (SELECT 1 FROM {qident(schema)}.{qident(cn)} c '
                        f'WHERE c."column" = {qlit(rc)} AND lower(c."value") = lower(m.{qident(rc)}) '
                        f'AND c."country" = {qlit(country)}) ')
            sql += f'ORDER BY u."embedding" <=> %s::vector LIMIT {self.HYBRID_LIMIT}'
            cur.execute(sql, [pv])
            outcols = [d[0] for d in cur.description]
            outrows = [["" if v is None else v for v in row] for row in cur.fetchall()]
            conn.commit()
        finally:
            conn.close()
        return {"question": question, "as_of": as_of,
                "sql": (f'SELECT {disp} FROM {qident(tn)} m '
                        f'JOIN {qident(un)} u ON u."__pk"=m."__pk" AND u."column"={qlit(plan["freetext"])} '
                        + (f'WHERE EXISTS (SELECT 1 FROM {qident(cn)} c WHERE lower(c."value")=lower(m.{qident(rc)}) '
                           f'AND c."country"={qlit(country)}) ' if (country and rc) else "")
                        + f'ORDER BY u."embedding" <=> embed({pred!r}) LIMIT {self.HYBRID_LIMIT}'),
                "result": {"columns": outcols, "rows": outrows}, "error": None,
                "routed": {"table": tn, "freetext_col": plan["freetext"],
                           "connected": [c for c, _ in plan["conn"]]},
                "meaning_join": {"country": country, "predicate": pred,
                                 "connected_bridge": cn, "unconnected_bridge": un},
                "provenance": None, "warnings": [], "dims": None,
                "model": "engine - unified encoder: PERSISTED connected bridge world join + unconnected semantic rank (pgvector <=>)"}

    # ---------------- clarify: detect a query that dropped part of the question + propose a rephrasing ----------------
    def _word_qid(self, w):
        """the world qid a single content word resolves to (exact normalized match in knowledgebase.\"words\" across the geo
        entity types), so _uncovered can tell a word is COVERED when its QID appears in the qid-keyed wikipedia SQL."""
        try:
            cur = self._rconn().cursor()
            cur.execute('SELECT qid FROM knowledgebase."words" WHERE norm=%s AND qid IS NOT NULL '
                        "AND type IN ('country','continent','city','state') LIMIT 1", (normalize_surface(w),))
            r = cur.fetchone()
            return r[0] if r else None
        except Exception:                                        # noqa: BLE001
            return None

    def _uncovered(self, question, sch, sql):
        """Content words whose meaning did NOT reach the SQL — the query silently dropped part of the question.
        Two failure modes this catches (not just the bare SELECT * one):
          - 'German sales' -> SELECT * : Germany never gets filtered, 'sales' never aggregated (BOTH dropped).
          - 'French sales' -> SELECT name WHERE France : France IS filtered, but 'sales' (a measure) produced no
            aggregate -> 'sales' is dropped. (Not degenerate, so the old gate missed it.)
        A word is COVERED if it resolves to an entity that appears in the SQL, or is a measure word AND the SQL
        aggregates. Returns the dropped words; empty = the query faithfully reflects the question."""
        import re as _re
        sqll = (sql or "").lower()
        has_agg = bool(_re.search(r'\b(sum|count|avg|min|max)\s*\(', sqll))
        def _forms(w):
            f = {w, w.rstrip("s"), w + "s"}
            if w.endswith("y"):
                f.add(w[:-1] + "ies")                        # city -> cities
            if w.endswith("ies"):
                f.add(w[:-3] + "y")
            return f
        sch_words = set()
        for c in sch:
            for nm in (str(c["table"]).lower(), str(c["name"]).lower()):
                for part in {nm} | set(nm.split("_")):
                    sch_words |= _forms(part)                # incl. plurals: a 'city' column also covers 'cities'
        # question / aggregate CUE words are realized by the OPERATOR (has_agg), not by a filter — they are never a
        # world entity, so excluding them stops _best_world_entity from spuriously matching e.g. 'how'/'many' to a
        # town and falsely reporting the COUNT query "dropped" them (which hijacked 'how many … in France' to clarify).
        # The world ENTITY-TYPE nouns (cities/countries/…) NAME the resolved type — never a dropped filter value —
        # so 'total amount for CITIES in France' must not report 'cities' dropped and hijack a correct join to clarify.
        CUE = {"how", "many", "much", "number", "list", "show", "give", "find", "get", "what", "which", "who", "whom",
               "where", "when", "average", "avg", "mean", "total", "sum", "count", "per", "each", "are", "were",
               "city", "cities", "country", "countries", "state", "states", "town", "towns", "place", "places",
               "nation", "nations", "element", "elements"}
        content = [w for w in _re.findall(r"[a-z]+", question.lower())
                   if w not in STOP and w not in CUE and len(w) > 1
                   and w not in sch_words and w.rstrip("s") not in sch_words]
        if not content:
            return []
        nonid = [c for c in sch if c.get("affinity") in ("INTEGER", "REAL") and not self._is_id(c["name"])]
        uv = self._encode(content)
        dropped = []
        for i, w in enumerate(content):
            if _re.search(r"\b" + _re.escape(w) + r"\b", sqll):  # the word literally appears in the SQL (a filter value
                continue                                         # like continent='Asia') -> it WAS used; covered. This
            wq = self._word_qid(w)                               # qid-keyed SQL: a word is COVERED if its resolved QID
            if wq and wq.lower() in sqll:                        # appears (wikipedia filters on qids, e.g. continent='Q46')
                continue
            ent = self._best_world_entity([w])                   # also catches continents/currencies _best_world_entity
            #                                                      doesn't resolve (it only knows country/city).
            if ent:
                if ent[1].lower() not in sqll:               # resolved to an entity the query did NOT filter on
                    dropped.append(w)
                continue                                     # entity present in the SQL -> used
            if nonid and not has_agg:                        # a measure word, but no aggregate applied -> dropped
                if max(_cos(uv[i], np.asarray(c["qvec"], np.float32)) for c in nonid) > 0.5:
                    dropped.append(w)
        return dropped

    def _best_world_entity(self, tokens, floor=0.6):
        """Best (token, canonical, type, sim) world-entity guess across tokens, even BELOW the 0.80 resolve
        threshold, so 'German' surfaces Germany (~0.7) for the rephrase. PREFERS country over city — a demonym
        like 'German'/'French' means the country, not some town literally named 'German'."""
        best_country, best_city = None, None
        for w in tokens:
            try:
                vec = Embedder.get().encode([w])[0]
            except Exception:                                # noqa: BLE001
                continue
            cc, cs = self._nn(vec, "country")
            if cc and cs >= floor and (best_country is None or cs > best_country[3]):
                best_country = (w, cc, "country", float(cs))
            ci, ci_s = self._nn(vec, "city")
            if ci and ci_s >= floor and (best_city is None or ci_s > best_city[3]):
                best_city = (w, ci, "city", float(ci_s))
        return best_country or best_city

    def _clarify(self, question, norm, fks, sch):
        """A best-guess UNAMBIGUOUS rephrasing + the bindings, from the model's SUB-THRESHOLD signals — so the user
        confirms an interpretation instead of getting a degenerate query. Returns {proposed, bindings} or None when
        there is no usable guess (a genuine 'list all customers' stays a plain SELECT *)."""
        import re as _re
        # tokens that name a table or column are SCHEMA, not world entities — exclude them so 'customers' isn't
        # bge-matched to some town (which would wrongly clarify a plain 'list all customers').
        sch_words = set()
        for t in norm:
            n = t["name"].lower(); sch_words |= {n, n.rstrip("s")}
        for c in sch:
            cn = c["name"].lower(); sch_words |= {cn, cn.rstrip("s")} | set(cn.split("_"))
        content = [w for w in _re.findall(r"[a-z]+", question.lower())
                   if w not in STOP and len(w) > 1 and w not in sch_words and w.rstrip("s") not in sch_words]
        if not content:
            return None
        ent = self._best_world_entity(content)               # ('german','Germany','country',0.70) | None
        if ent and _re.fullmatch(r"[Qq]\d+", str(ent[1] or "")):
            ent = None                                       # words row whose canonical is a raw Wikidata QID (data
        ent_tok = ent[0] if ent else None                    # gap): never surface a QID in a human-facing rephrase
        _, scores = self.read_op_model(norm, question, fks)
        sum_s, avg_s, cnt_s = scores.get("SUM", 0.0), scores.get("AVG", 0.0), scores.get("COUNT", 0.0)
        nonid = [c for c in sch if c.get("affinity") in ("INTEGER", "REAL") and not self._is_id(c["name"])]
        qv = self._encode([question])[0]
        measure = max(nonid, key=lambda c: _cos(qv, c["qvec"])) if nonid else None
        mword, mscore = None, 0.0
        cand = [w for w in content if w != ent_tok]
        if measure is not None and cand:
            mv = self._encode(cand); mvec = np.asarray(measure["qvec"], np.float32)
            mword, mscore = max(((w, _cos(mv[i], mvec)) for i, w in enumerate(cand)), key=lambda z: z[1])
        table = norm[0]["name"]
        bindings, op_word, measure_part = [], None, None
        if measure is not None and (mscore > 0.45 or sum_s > 0.4 or avg_s > 0.4):
            op_word = "average" if (avg_s > sum_s and avg_s > 0.4) else "total"
            measure_part = measure["name"]
            bindings.append({"token": mword or measure["name"], "kind": "measure", "target": measure["name"],
                             "op": "AVG" if op_word == "average" else "SUM",
                             "score": round(float(max(mscore, sum_s, avg_s)), 2)})
        elif cnt_s > 0.5:
            op_word = "count"
        if ent:
            bindings.append({"token": ent[0], "kind": ent[2], "target": ent[1], "score": round(ent[3], 2)})
        if ent is None and measure_part is None and op_word != "count":
            return None                                      # nothing usable to propose
        where = f" in {ent[1]}" if ent else ""
        if op_word == "count":
            proposed = f"how many {table}{where}"
        elif measure_part:
            proposed = f"{op_word} {measure_part}{where}"
        elif ent:
            proposed = f"{table}{where}"                     # just the world filter (e.g. 'customers in Germany')
        else:
            return None
        return {"proposed": proposed.strip(), "bindings": bindings}

    def _country_name_for_qid(self, qid):
        """A resolved country comes back as a QID (world.words stores qids), but the connected bridge's `country`
        column holds the canonical country NAME (canon_country). Map qid -> name so the hybrid filter and the city
        context-disambiguation compare name-vs-name. Returns None if the qid isn't a known country."""
        import re
        if not qid:
            return None
        if not re.match(r"^Q\d+$", str(qid)):
            return qid                                                # already a name (defensive) -> pass through
        try:
            cur = self._rconn().cursor()
            cur.execute('SELECT canonical FROM knowledgebase."words" WHERE type=\'country\' AND qid=%s '
                        'AND canonical IS NOT NULL LIMIT 1', (qid,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None
        except Exception as e:                                        # noqa: BLE001 — a lookup miss must not hard-fail the world path
            print(f"[knowledge_query] country name lookup failed for {qid!r}: {e}", flush=True)
            return None

    def serve(self, tables, question, as_of=None, schema=None):
        """Hybrid structured+semantic retrieval when the question has a free-text predicate AND the data has a
        free-text column AND it is not an aggregate; otherwise delegate to EntityQuery (which uses the unified
        operator via read_op_all). Any hybrid error falls back to EntityQuery so the world path never hard-fails."""
        norm, fks = self.ingest(tables)
        sch, _, _ = self.schema(norm, fks)
        is_agg = self.read_op_all(question, sch) is not None
        if is_agg and schema:                                         # NON-GEO world join (hospital/software/... + lazy fill)
            try:
                ngp = self._nongeo_plan(norm, question)
                if ngp:
                    return self._serve_world_type(norm, question, sch, ngp, schema)
            except Exception as e:                                    # noqa: BLE001 — fall through to the geo/delegate path
                import traceback
                print(f"[knowledge_query] non-geo world serve failed, falling through: {e!r}", flush=True); traceback.print_exc()
        cr = None if is_agg else self._resolve(question, "country")   # (country QID, sim, surface) | None — resolved ONCE
        pred = "" if is_agg else self._semantic_predicate(question, [cr[2]] if cr else [])
        plan = next((p for p in (self._table_plan(t) for t in norm) if p), None) if pred else None
        if plan and pred and schema:
            try:
                # _resolve returns a country QID, but the connected bridge stores + filters/disambiguates on the
                # country NAME (canon_country). Map qid -> canonical name here; otherwise "in France" compares
                # 'Q142' against 'France' and the EXISTS matches nothing (silent empty result).
                cname = self._country_name_for_qid(cr[0]) if cr else None
                if cr and not cname:
                    cname = cr[2]                                      # last resort: the surface the user typed
                return self._serve_hybrid(norm, fks, sch, question, pred, plan,
                                          cname, as_of, schema)
            except Exception as e:                                   # noqa: BLE001 — never hard-fail the world path
                import traceback
                print("hybrid serve failed, delegating to EntityQuery:", e, flush=True)
                traceback.print_exc()
        # Delegate the aggregate / plain-world-join path to EntityQuery EXPLICITLY (not super()): in this MRO
        # TableQuery precedes EntityQuery (EncoderQuery pulls TableQuery in early), so super().serve would hit the
        # 2-arg TableQuery.serve. EntityQuery.serve's own super() is relative to EntityQuery and correctly chains
        # RoutedQuery->PgQuery->KnowledgeTableQuery (skipping TableQuery). read_op_all inside that chain still resolves
        # to EncoderQuery's metric-space operator via MRO.
        res = EntityQuery.serve(self, tables, question, as_of=as_of, schema=schema)
        # Clarify gate (COVERAGE): if the query silently DROPPED part of the question — a degenerate SELECT *
        # ('German sales'), OR a measure word with no aggregate ('French sales' -> SELECT name WHERE France) — offer
        # a best-guess unambiguous rephrasing (from the model's sub-threshold signals) for the user to confirm,
        # instead of "bullshitting" a wrong query. The clarify UI lets the user confirm or edit before re-running.
        if schema:
            try:
                dropped = self._uncovered(question, sch, (res or {}).get("sql"))
            except Exception as e:                           # noqa: BLE001 — the gate must never break the world path
                print("coverage check failed:", e, flush=True); dropped = []
            if dropped:
                try:
                    c = self._clarify(question, norm, fks, sch)
                except Exception as e:                       # noqa: BLE001
                    print("clarify failed:", e, flush=True); c = None
                if c and c["proposed"].strip().lower() != (question or "").strip().lower():
                    return {"question": question, "as_of": as_of, "clarify": True,
                            "original_sql": (res or {}).get("sql"), "proposed": c["proposed"],
                            "bindings": c["bindings"], "dropped": dropped,
                            "model": "engine - clarify (the query dropped part of the question)"}
        return res


def _demo():
    if not os.environ.get("KB_PG_PASSWORD"):
        print("set KB_PG_PASSWORD to run the live world-DB demo"); return
    schema = os.environ.get("AUTH_TEST_SUB", "world_demo")
    Q = KnowledgeQuery()
    print(f"loaded KnowledgeQuery (hdim={Q.hdim}); schema={schema}\n")
    CUST = {"name": "customers", "columns": ["name", "city", "remarks"], "rows": [
        ["Ada", "Paris", "package arrived late and damaged, terrible delivery"],
        ["Lin", "Lyon", "great product, very happy with the quality"],
        ["Bo", "Berlin", "shipping was slow and the box was crushed"],
        ["Sam", "Nice", "excellent service, fast and smooth"],
        ["Mai", "Tokyo", "the courier lost my parcel, awful logistics"],
        ["Eve", "Munich", "love it, would buy again"]]}
    for q in ["who complained about bad delivery in France", "who complained about bad delivery",
              "how many customers in France"]:
        res = Q.serve([CUST], q, schema=schema)
        print(f"Q: {q}\n   model={res['model'].split(' - ')[0]}\n   sql={res.get('sql')}")
        rr = (res.get("result") or {}).get("rows") or []
        for row in rr[:5]:
            print("   ", row)
        print()


if __name__ == "__main__":
    _demo()
