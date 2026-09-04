"""KnowledgeQuery — the UNIFIED ENCODER wired into the LIVE /api/knowledge path, end to end.

This realizes the unified-encoder objective in production (not just /api/dimension analyze):
  * OPERATOR + OPERAND from the metric space  — inherited EncoderQuery.read_op_all (no MEASURE_NOUNS/table_noun);
    the delegate (aggregate / pure world-join) path is EntityQuery.serve, which calls THIS read_op_all via MRO.
  * BRIDGE TABLES persisted per user on Postgres (the thesis: "an interpretable model is a database"):
      "<t> connected to wikipedia"   = legacy bridge name for resolved FKs (cell -> world key + country), via bge +
                                       knowledgebase.words (exact entity resolution; same-space NOT required —
                                       the join is on a string key).
      "<t> unconnected to wikipedia" = legacy bridge name for a unified-encoder vector(896) per free-text cell
                                       (remarks, notes, …),
                                       so a free-text MEANING is kept as an embedding.
  * HYBRID structured+semantic query — "who complained about bad delivery in France" =
        connected:   country = 'France'                      (world join, bge-resolved)
      + unconnected: remarks <=> embed('bad delivery')       (pgvector cosine, UNIFIED encoder both sides)
    The predicate vector and the stored column vectors come from the SAME unified encoder (EncoderQuery._encode),
    so `<=>` is a valid same-space cosine — the reason the encoder had to be unified first.

Class graph:  KnowledgeQuery(EncoderQuery, KnowledgeBridgeMixin, EntityQuery)
  The bridge mixin owns persistence and hybrid pgvector execution; this module owns routing, planning,
  clarification, and request orchestration.
    - read_op_all / read_op_model / _is_id  resolve to EncoderQuery (the metric-space operator), NOT keywords.
    - serve / meaning_filter / _world_joins / route / _resolve  resolve to EntityQuery (the world machinery + bge).
    - schema / _encode / _layers  resolve to TableQuery, but run on the UNIFIED qwen (overlaid in __init__).
"""
from __future__ import annotations
import os

import numpy as np

from engine.config import DATA_DIR, kb_model_route_enabled
from engine.tables import qlit
from engine.entities import EntityQuery, WORLD_TABLE_TYPE
from engine.embeddings import Embedder, pgvector_literal, normalize_surface
from engine.encoder_overlay import EncoderQuery, load_encoder
from engine.knowledge_bridges import KnowledgeBridgeMixin
from engine.knowledge_typing import KnowledgeTypingMixin
from engine.bridge import STOP
from engine.currency_intent import (
    currency_conversion_target, currency_conversion_words, currency_rate_attribute,
)
from engine.calculations import calculation_clarify
from engine.numeric import parse_decimal, wire_decimal


def _cos(a, b):
    a = np.asarray(a, np.float32); b = np.asarray(b, np.float32)
    return float(a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9))


def _is_num(v):
    try:
        parse_decimal(v); return True
    except (ValueError, TypeError):
        return False


class KnowledgeQuery(EncoderQuery, KnowledgeBridgeMixin, KnowledgeTypingMixin, EntityQuery):
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

    # ---------------- Schema.org evidence + deterministic source grounding ----------------
    def route(self, table):
        """Route columns with calibrated Schema.org evidence plus exact source keys.

        The model may propose a servable class, but a world join is authorized only by
        source-key grounding. The inherited exact membership route supplies coverage
        when the model abstains. Captured evidence is the computation actually used.
        """
        sig = self._table_sig(table)
        # ONE cache holding (routes, typing) together. Two dicts keyed by the same signature had to be
        # written, read and purged in lockstep at three sites by discipline alone; any future writer that
        # updated one and not the other would serve cached routes with stale or missing evidence, and no
        # test would notice because the routing output would be unchanged.
        cache = self.__dict__.setdefault("_route_cache", {})               # per (schema, values): router runs ONCE
        if sig in cache:
            routes, typing = cache[sig]
            self._emit_typing(typing)
            return dict(routes)
        routes, typing = {}, []
        model_routes, model_typing = self._schema_model_routes(table)
        routes.update(model_routes)
        typing.extend(model_typing)
        # This is deliberately the exact source-key helper, not ``super().route``:
        # the latter invokes the historical anchored family path. Production has one
        # learned class router; its abstentions fall back directly to source evidence.
        source_routes = self._value_membership_routes(table)
        for key, world_table in source_routes.items():
            routes.setdefault(key, world_table)
            record = next((item for item in typing
                           if (item["table"], item["column"]) == key), None)
            grounding = {
                "source": "wikidata",
                "index": "knowledgebase.words",
                "method": "exact_normalized_membership",
            }
            if record is not None:
                record["grounded_to"] = world_table
                record["grounding"] = grounding
            else:
                typing.append({
                    "table": key[0], "column": key[1], "kind": "source_grounding",
                    "family": "place", "frac": None, "geo": True,
                    "grounded_to": world_table, "grounding": grounding,
                    "class": None, "class_name": None,
                    "ontology_version": None, "model_artifact_sha256": None,
                    "evidence": [],
                })
        if len(cache) > 100:
            cache.clear()
        cache[sig] = (dict(routes), typing)
        self._emit_typing(typing)
        return routes

    # ---------------- NON-GEO world join over pre-synchronized facts ----------------
    # The faithful Wikidata tables (knowledgebase."hospital"/"software"/...) join like the geo ones: resolve the uploaded
    # cell -> the type's qid (knowledgebase.words, type=<leaf>), JOIN knowledgebase."<leaf>" ON qid, filter by a world attribute
    # (country), aggregate the uploaded metric. Missing facts abstain; serving never fetches or writes them.
    def _resolve_world_qid(self, value, label, type_qid):
        """value -> the world qid. knowledgebase.words.type stores the EXACT Wikidata label (what the offline
        syncs insert), NOT the snake routing leaf — so look it up by that exact label, else a
        multi-word type ('academic journal' vs the routing 'academic_journal') misses the fast path forever.
        Exact norm match, then bge NN. A miss remains unresolved until an offline source sync supplies it."""
        cur = self._rconn().cursor()
        cur.execute('SELECT label FROM knowledgebase."types" WHERE qid=%s', (type_qid,))
        _r = cur.fetchone()
        wl = (str(_r[0]) if _r and _r[0] else label)                  # the exact label used by the offline projection
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
        return None

    def _world_type_map(self):
        """{snake(leaf label) -> type qid} for the mirrored non-geo tables, from taxonomy.csv. Cached."""
        m = getattr(self, "_wtmap", None)
        if m is None:
            import csv as _csv
            from engine.taxonomy import snake
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
        """Plan a non-geo world join from calibrated evidence and exact source keys.

        Schema.org evidence is captured when available. The actual fine type is
        established independently by a majority of exact ``knowledgebase.words``
        matches and an explicit type mention in the question. Thus model abstention
        cannot remove deterministic coverage, and model output cannot invent a join.
        """
        import re as _re
        cr = self._resolve(question, "country")
        if not cr:                                                        # only the country-filtered non-geo agg for now
            return None
        ql = question.lower()
        r = None
        if kb_model_route_enabled():
            try:
                r = self._router()
            except Exception as exc:                                     # noqa: BLE001
                print(f"[knowledge_query] schema router unavailable -> source grounding only: {exc!r}", flush=True)
        cur = self._rconn().cursor()
        for t in norm:
            for ci, col in enumerate(t["columns"]):
                cells = [str(rw[ci]) for rw in t["rows"] if ci < len(rw) and rw[ci] not in (None, "")]
                if len(cells) < 3:                                        # entity names are long but model+grounding gate
                    continue
                if r is not None:
                    try:
                        evidence = r.route(cells, header=col)
                    except Exception as exc:                              # noqa: BLE001
                        print(f"[knowledge_query] column evidence unavailable for {col!r}: {exc!r}", flush=True)
                        evidence = None
                    if evidence:
                        self._emit_typing([{
                            "table": t["name"], "column": col,
                            "family": evidence["family"], "frac": evidence["frac"],
                            "geo": evidence["geo"], "grounded_to": None,
                            "class": evidence.get("class"),
                            "class_name": evidence.get("class_name"),
                            "class_threshold": evidence.get("class_threshold"),
                            "class_score_model": evidence.get("class_score_model"),
                            "class_bias": evidence.get("class_bias"),
                            "ontology_version": evidence.get("ontology_version"),
                            "model_artifact_sha256": evidence.get("model_artifact_sha256"),
                            "evidence": evidence.get("evidence", []),
                        }])
                wl, tqid = self._dominant_nongeo_type(cells)             # FINE type = dominant knowledgebase.words type the cells resolve to
                if not (wl and tqid):
                    continue
                # the QUESTION must name this type ('...for hospitals...') — 'total amount in France' names no type,
                # so a person-name column (which grounds as some entity type) can't hijack the plain geo aggregate.
                if not any(_re.search(r"\b" + w + r"s?\b", ql) for w in wl.replace("_", " ").split() if len(w) > 3):
                    continue
                cur.execute("SELECT 1 FROM information_schema.columns WHERE table_schema='knowledgebase' "
                            "AND table_name=%s AND column_name='country'", (wl,))
                if not cur.fetchone():
                    continue                                             # need knowledgebase."<wl>".country to filter
                return {"table": t, "col": col, "label": wl, "qid": tqid, "country": cr[0], "cells": cells}
        return None

    def _dominant_nongeo_type(self, cells):
        """FINE type of a non-geo entity column = the dominant knowledgebase.words `type` its cells resolve to (exact
        norm; excludes the geo types). The fine type and QID come from source resolution, independently of the
        learned class evidence. Returns (exact-Wikidata-label, type-qid) or (None, None). This source-grounded
        fallback is permitted even when the
        Schema.org model abstains; it cannot invent a type because every accepted cell matches a stored source key."""
        norms = sorted({normalize_surface(str(c)) for c in cells if str(c).strip()})
        if len(norms) < 2:
            return None, None
        cur = self._rconn().cursor()
        cur.execute("SELECT type, COUNT(DISTINCT norm) FROM knowledgebase.\"words\" WHERE norm = ANY(%s) "
                    "AND type NOT IN ('city','country','state','type') GROUP BY type ORDER BY 2 DESC LIMIT 1", (norms,))
        row = cur.fetchone()
        # >=50% of the DISTINCT cells must resolve to ONE non-geo type. The question must separately name that type,
        # so this deterministic fallback remains fail-closed when Schema.org classification abstains.
        if not row or row[1] < max(2, 0.5 * len(norms)):
            return None, None
        wl = row[0]
        cur.execute('SELECT qid FROM knowledgebase."types" WHERE label=%s LIMIT 1', (wl,))
        q = cur.fetchone()
        return (wl, q[0]) if q else (None, None)

    def _serve_world_type(self, norm, question, sch, plan, schema):
        """Aggregate an uploaded NON-GEO table joined to its faithful Wikidata world table, filtered by country.
        e.g. hospitals.csv(hospital, beds) + 'total beds for hospitals in United States' -> resolve each hospital to
        a pre-synchronized knowledgebase.\"hospital\" row, keep those whose .country = 'United States', SUM(beds)."""
        t = plan["table"]; label = plan["label"]; country = plan["country"]
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
                    mv = parse_decimal(rw[mi])
                except (ValueError, TypeError):
                    mv = None
            pairs.append((wq, mv))

        cur = self._rconn().cursor()
        cur.execute('SELECT label FROM knowledgebase."types" WHERE qid=%s', (plan["qid"],))
        _r = cur.fetchone(); wl = (str(_r[0]) if _r and _r[0] else label)[:63]   # table = the EXACT Wikidata label
        qids = sorted({wq for wq, _ in pairs})
        # country filter is qid = qid (plan["country"] is a country QID; w."country" is the country's qid FK)
        cur.execute(f'SELECT qid FROM knowledgebase."{wl}" WHERE qid = ANY(%s) AND lower("country") = lower(%s)', (qids, country))
        keep = {r[0] for r in cur.fetchall()}
        hit = [(wq, mv) for wq, mv in pairs if wq in keep]
        if op == "COUNT":
            val = len(hit)
        elif op == "SUM":
            val = sum((mv for _wq, mv in hit if mv is not None), start=parse_decimal(0))
        else:                                                             # AVG
            mvs = [mv for _wq, mv in hit if mv is not None]
            val = sum(mvs, start=parse_decimal(0)) / len(mvs) if mvs else parse_decimal(0)
        if not isinstance(val, int):
            val = wire_decimal(val)
        disp = f'{op}({measure})' if measure else 'COUNT(*)'
        sql = (f'SELECT {disp} FROM "{t["name"]}" u JOIN knowledgebase."{wl}" w ON w.qid = resolve(u."{plan["col"]}") '
               f'WHERE w."country" = {qlit(country)}')
        return {"question": question, "as_of": None, "sql": sql,
                "result": {"columns": [disp], "rows": [[val]]},          # "columns" (NOT "cols") — the client render +
                "model": f'engine - non-geo world join (pre-synchronized knowledgebase."{wl}")'}

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

    # ---------------- clarify: detect a query that dropped part of the question + propose a rephrasing ----------------
    def _word_qid(self, w):
        """the world qid a single content word resolves to (exact normalized match in knowledgebase.\"words\" across the geo
        entity types), so _uncovered can tell a word is COVERED when its QID appears in the qid-keyed knowledgebase SQL."""
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
               "nation", "nations", "element", "elements", "atomic", "has", "highest", "lowest", "largest",
               "smallest", "most", "least", "maximum", "minimum", "max", "min", "top", "bottom"}
        content = [w for w in _re.findall(r"[a-z]+", question.lower())
                   if w not in STOP and w not in CUE and len(w) > 1
                   and w not in sch_words and w.rstrip("s") not in sch_words]
        # Target words are covered only when SQL uses the exact direct-rate column for that
        # target. Unrelated arithmetic or a rate for another currency cannot bypass clarify.
        currency_target = currency_conversion_target(question)
        if (currency_target is not None and has_agg and "*" in sqll
                and currency_rate_attribute(currency_target) in sqll):
            realized = currency_conversion_words(currency_target)
            content = [word for word in content if word not in realized]
        if not content:
            return []
        nonid = [c for c in sch if c.get("affinity") in ("INTEGER", "REAL") and not self._is_id(c["name"])]
        uv = self._encode(content)
        dropped = []
        for i, w in enumerate(content):
            if _re.search(r"\b" + _re.escape(w) + r"\b", sqll):  # the word literally appears in the SQL (a filter value
                continue                                         # like continent='Asia') -> it WAS used; covered. This
            wq = self._word_qid(w)                               # qid-keyed SQL: a word is COVERED if its resolved QID
            if wq and wq.lower() in sqll:                        # appears (knowledgebase filters on qids, e.g. continent='Q46')
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
        """A resolved country comes back as a QID (knowledgebase.words stores qids), but the connected bridge's `country`
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

    def serve(self, tables, question, as_of=None, schema=None, explicit_fks=()):
        """Hybrid structured+semantic retrieval when the question has a free-text predicate AND the data has a
        free-text column AND it is not an aggregate; otherwise delegate to EntityQuery (which uses the unified
        operator via read_op_all). Any hybrid error falls back to EntityQuery so the world path never hard-fails."""
        norm, fks = self.ingest(tables, explicit_fks=explicit_fks)
        sch, _, _ = self.schema(norm, fks)
        is_agg = self.read_op_all(question, sch) is not None
        if is_agg and schema:                                         # NON-GEO world join over synchronized facts
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
        res = EntityQuery.serve(self, tables, question, as_of=as_of, schema=schema,
                                explicit_fks=explicit_fks)
        if isinstance(res, dict) and res.get("clarify"):
            return res
        calculations = tuple((res or {}).get("calculations") or ()) if isinstance(res, dict) else ()
        if calculations and any(row.get("status") != "satisfied" for row in calculations):
            return calculation_clarify(question, res, calculations)
        currency = (res or {}).get("currency") if isinstance(res, dict) else None
        # Clarify gate (COVERAGE): if the query silently DROPPED part of the question — a degenerate SELECT *
        # ('German sales'), OR a measure word with no aggregate ('French sales' -> SELECT name WHERE France) — offer
        # a best-guess unambiguous rephrasing (from the model's sub-threshold signals) for the user to confirm,
        # instead of "bullshitting" a wrong query. The clarify UI lets the user confirm or edit before re-running.
        if schema:
            try:
                dropped = self._uncovered(question, sch, (res or {}).get("sql"))
                if currency and currency.get("status") == "satisfied":
                    realized = currency_conversion_words(currency["target"])
                    dropped = [word for word in dropped if word not in realized]
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
