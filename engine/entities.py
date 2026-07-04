"""Entity resolution by EMBEDDING NEAREST-NEIGHBOUR, replacing hardcoded alias tables and every lower()
string hack.

A filter value that is ABSENT from the upload ("cities in US") is resolved like this:
  spaCy parses the prompt -> candidate surface forms ("US")
  bge-small embeds each candidate
  cosine NN against the world `words` vector index, RESTRICTED to the filter attr's entity type (country)
  if the best match clears a calibrated threshold -> use that canonical word ("United States") in the WHERE
No alias list, no lower(), no string matching. "US"/"USA"/"UK"/"Deutschland" resolve by meaning; the preposition
"in" and non-entities ("Indiana") stay below threshold and fall through.

This overrides ONLY value resolution (WorldTableQuery._find_value) plus the cell-side bridge (uploaded city
values -> world PK via a per-upload bridge). Everything else — routing, joins, SQL assembly, multi-table FK,
the analyze view — is inherited unchanged from the layers below.
"""
from __future__ import annotations
import re

from engine.config import DATA_DIR
from engine.resolve_base import RoutedQuery
from engine.pg import _pg
from engine.world_tables import qident, qlit
from engine.embeddings import Embedder, pgvector_literal, normalize_surface

# filter attr (as named in word_*.json) -> the entity `type` it is matched against in world."words"
ENTITY_TYPES = {"country": "country", "nation": "country", "continent": "continent",
                "state": "state", "american_state": "state", "province": "state", "region": "state",
                "element": "element", "city": "city", "municipality": "city"}
# function words that are never an entity — dropped as candidates so we don't waste NN lookups (the threshold
# would reject them anyway, but "in"->India 0.68 etc. is noise we skip up front).
STOP = {"in", "the", "of", "a", "an", "for", "to", "by", "on", "at", "with", "and", "or", "is", "are",
        "how", "many", "much", "total", "sum", "average", "count", "number", "all", "each", "per", "their"}
# wikipedia table (exact Wikidata label) -> the `type` its rows carry in world."words" (cell-side bridge resolution).
# The tables are the qid-keyed wikipedia."city"/"country" (via WORLD_NAMES); the planner resolves both the cell
# bridge and the filter to QIDS, so every world join + filter is qid PK/FK.
WORLD_TABLE_TYPE = {"city": "city", "country": "country"}
TYPE_TO_FRIENDLY = {v: k for k, v in WORLD_TABLE_TYPE.items()}   # 'city' -> 'city' (the wikipedia table)
VALUE_ROUTE_MIN = 0.80   # a column routes to a world table when >= this fraction of its cells resolve to that type


class EntityQuery(RoutedQuery):
    """RoutedQuery + embedding-NN value resolution (bge-small over the world `words` pgvector index)."""

    THRESH = 0.80            # prompt-side fuzzy-fallback floor; exact altLabel match handles conventional aliases
    FUZZY_THRESH = 0.88      # cell-side TYPO floor (tight): aliases come from altLabel DATA, not loose fuzzy

    def __init__(self, deploy_dir=DATA_DIR):
        super().__init__(deploy_dir)
        self._nlp = None
        self._rcn = None     # cached resolution connection (1 per instance)

    # ---- helpers ----
    def _spacy(self):
        if self._nlp is None:
            import spacy
            self._nlp = spacy.load("en_core_web_md", disable=["lemmatizer"])
        return self._nlp

    def _rconn(self):
        # AUTOCOMMIT: this cached connection issues many independent read SELECTs (resolution / routing / grounding)
        # interleaved with idempotent bridge writes. Without autocommit psycopg2 opens an implicit transaction on the
        # first statement and leaves it "idle in transaction" until an explicit commit — holding read locks on
        # world."words"/the bridge tables. Two service instances serving the SAME per-user sub then wedge each other
        # (a concurrent ALTER TABLE on the bridge blocks ~indefinitely on that relation lock; a measured 833s stall).
        # Every write here is idempotent (CREATE/ADD COLUMN IF NOT EXISTS, DELETE+INSERT refresh, ensure_entity ON
        # CONFLICT DO NOTHING), so per-statement autocommit is safe and the explicit .commit() calls become no-ops.
        if self._rcn is None or self._rcn.closed:
            self._rcn = _pg()
            self._rcn.autocommit = True
        return self._rcn

    def _candidates(self, question):
        """surface forms worth resolving: spaCy place-ish entities + noun chunks + proper nouns + bare alpha
        tokens. Deduped (case-insensitive), function words dropped, capped. The threshold does the real filtering;
        this just bounds how many NN lookups we issue."""
        raw = []
        try:
            doc = self._spacy()(question)
            raw += [e.text for e in doc.ents if e.label_ in {"GPE", "LOC", "NORP", "FAC", "ORG", "PRODUCT"}]
            raw += [nc.text for nc in doc.noun_chunks]
            raw += [t.text for t in doc if t.pos_ in {"PROPN", "NOUN"} or t.is_alpha]
        except Exception:                                         # spaCy missing/broken -> plain n-gram fallback
            toks = re.findall(r"[A-Za-z][A-Za-z.\-]*", question)
            raw += toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]
        out, seen = [], set()
        for c in raw:
            c = c.strip()
            cl = c.lower()
            if len(cl) < 2 or cl in STOP or cl in seen:
                continue
            seen.add(cl); out.append(c)
        return out[:12]

    def _nn(self, vec, type_):
        lit = pgvector_literal(vec)
        cur = self._rconn().cursor()
        cur.execute('SELECT qid, 1-(embedding <=> %s::vector) AS sim FROM world."words" '            # QID, not the name —
                    'WHERE type=%s AND qid IS NOT NULL ORDER BY embedding <=> %s::vector LIMIT 1', (lit, type_, lit))
        r = cur.fetchone()
        return (r[0], float(r[1])) if r else (None, -1.0)                                            # the resolved qid

    def _resolve(self, question, type_):
        """resolve `question` to a canonical world PK of `type_`. HYBRID:
        (1) normalized-EXACT match of a candidate surface form against the words index (deterministic — this is
            what nails US/USA/UK/Holland/the-United-States, which embeddings get wrong); longest candidate first,
            and a norm that maps to >1 canonical is skipped as ambiguous;
        (2) embedding NN fallback for anything not matched exactly (typos / novel forms), gated by THRESH.
        Returns (canonical, score, surface) or None."""
        cands = self._candidates(question)
        if not cands:
            return None
        cur = self._rconn().cursor()
        norms = {c: normalize_surface(c) for c in cands}
        uniq = sorted({n for n in norms.values() if n})
        by_type = {}                                              # norm -> {type: {canonical}} across ALL types
        if uniq:
            cur.execute('SELECT norm, type, qid FROM world."words" WHERE norm = ANY(%s) AND qid IS NOT NULL', (uniq,))
            for nm, ty, q_ in cur.fetchall():
                by_type.setdefault(nm, {}).setdefault(ty, set()).add(q_)      # collect QIDS — resolve to qid, not name
        for c in sorted(cands, key=lambda x: -len(x)):           # (1) exact match of the requested type, longest first
            cs = by_type.get(norms[c], {}).get(type_)
            if cs and len(cs) == 1:
                return (next(iter(cs)), 1.0, c)
        # (2) fuzzy fallback — ONLY for candidates that exact-match NOTHING. A token that IS a known state/city/etc
        # (Indiana, Houston) is that entity, not a typo of a country, so it must not fuzzy-match a country.
        fuzzy = [c for c in cands if not by_type.get(norms[c])]
        if not fuzzy:
            return None
        vecs = Embedder.get().encode(fuzzy)
        best = (None, -1.0, None)
        for c, v in zip(fuzzy, vecs):
            w, sim = self._nn(v, type_)
            if sim > best[1]:
                best = (w, sim, c)
        return best if best[1] >= self.THRESH else None

    # ---- overrides ----
    def serve(self, tables, question, as_of=None, schema=None):
        self._q_orig = question                      # original-case prompt for spaCy + bge (low_q is lowercased)
        return super().serve(tables, question, as_of=as_of, schema=schema)

    def _find_value(self, low_q, w):
        """resolve a where value via embedding NN over the world `words` index for any ENTITY-typed filter attr;
        fall back to the plain matcher (bypassing the alias table) for non-entity attrs."""
        q = getattr(self, "_q_orig", None) or low_q
        best = None                                  # (attr, canonical, sim)
        for attr in w.get("filter_attrs", []):
            t = ENTITY_TYPES.get(attr)
            if not t:
                continue
            r = self._resolve(q, t)
            if r and (best is None or r[1] > best[2]):
                best = (attr, r[0], r[1])
        if best:
            return best[0], best[1]
        return super(RoutedQuery, self)._find_value(low_q, w)   # skip the alias table -> plain string match

    # ---- routing: deterministic VALUE-MEMBERSHIP (header-independent) over the model fallback ----
    def _value_membership_routes(self, table):
        """A column routes to the world table whose `type` >= VALUE_ROUTE_MIN of its cell VALUES resolve to in the
        `words` index (exact normalized membership) — regardless of the header ('Countries' / 'Country code' /
        'Nation' all route by their values). Column-level aggregation averages out single-value collisions
        (IN = India vs the state Indiana). -> {(table, col): friendly_world_table}."""
        base = table["name"]
        cur = self._rconn().cursor()
        routes = {}
        for ci, col in enumerate(table["columns"]):
            cells = [str(r[ci]) for r in table["rows"] if ci < len(r) and r[ci] not in (None, "")]
            if len(cells) < 3:
                continue
            norms = [normalize_surface(c) for c in cells]
            uniq = sorted({n for n in norms if n})
            if not uniq:
                continue
            cur.execute('SELECT DISTINCT norm, type FROM world."words" WHERE norm = ANY(%s) '
                        "AND type IN ('city','country','state','element','continent')", (uniq,))
            ntypes = {}
            for nm, ty in cur.fetchall():
                ntypes.setdefault(nm, set()).add(ty)
            counts = {}
            for n in norms:
                for ty in ntypes.get(n, ()):                      # a cell may match >1 type (Georgia); count each
                    counts[ty] = counts.get(ty, 0) + 1
            if not counts:
                continue
            best_ty, best = max(counts.items(), key=lambda kv: kv[1])
            friendly = TYPE_TO_FRIENDLY.get(best_ty)
            if best / len(cells) >= VALUE_ROUTE_MIN and friendly in self.words:
                routes[(base, col)] = friendly
        return routes

    def route(self, table):
        """Deterministic value-membership routing takes priority (data-driven, header-independent); the inherited
        model routing fills columns whose values aren't in the world index (out-of-world entities the model
        still recognizes by shape)."""
        ace = super().route(table)
        return {**ace, **self._value_membership_routes(table)}

    # ---- cell-side bridge (uploaded value -> world PK), the Paris->Paris-FR slice ----
    def _cell_bridge_sql(self, norm, mtab, route_col, wtype, ctx_country=None):
        """Build the bridge as ONE SQL subquery yielding (cell, canon). Exact normalized matches are a cheap batch
        lookup; the FUZZY remainder is resolved by a SET-BASED in-SQL nearest-neighbour join — the cell vectors are
        materialized into the query and POSTGRES does the `<=>` similarity search over world."words" (HNSW), with
        the threshold as a distance filter. Python only turns cell text into a bge vector; the search + join run in
        Postgres. COALESCE keeps unmatched cells as identity (== the old lower()=lower() behaviour). When
        `ctx_country` is set (the query filters to a country), BOTH exact and fuzzy resolution are constrained to
        that country (props->>'country') — context-aware same-name disambiguation. -> SQL | None."""
        t = next((x for x in norm if x["name"] == mtab), None)
        if not t or route_col not in t["columns"]:
            return None
        ci = t["columns"].index(route_col)
        cells = list({str(r[ci]) for r in t["rows"] if r[ci] not in (None, "")})
        if not cells:
            return None
        cur = self._rconn().cursor()
        norms = {c: normalize_surface(c) for c in cells}
        uniq = sorted({n for n in norms.values() if n})
        ctx_sql = f" AND w.props->>'country' = {qlit(ctx_country)}" if ctx_country else ""
        exact = {}
        if uniq:                                                  # one batch query, NOT per-cell
            q = ('SELECT w.norm, w.canonical FROM world."words" w WHERE w.type=%s AND w.norm = ANY(%s)' + ctx_sql)
            cur.execute(q, (wtype, uniq))
            tmp = {}
            for nm, cn_ in cur.fetchall():
                tmp.setdefault(nm, set()).add(cn_)
            exact = {nm: next(iter(cs)) for nm, cs in tmp.items() if len(cs) == 1}   # unique exact only
        exact_rows, fuzzy = [], []
        for c in cells:
            e = exact.get(norms[c])
            (exact_rows.append(f"({qlit(c)}, {qlit(e)})") if e else fuzzy.append(c))
        parts = []
        if exact_rows:
            parts.append(f"SELECT * FROM (VALUES {', '.join(exact_rows)}) AS ex(cell, canon)")
        if fuzzy:                                                 # similarity search runs IN POSTGRES (LATERAL <=>)
            vecs = Embedder.get().encode(fuzzy)
            vlits = ", ".join(f"({qlit(c)}, {qlit(pgvector_literal(v))}::vector)" for c, v in zip(fuzzy, vecs))
            maxd = 1.0 - self.THRESH                              # cosine distance <= 1-thresh  <=>  cosine sim >= thresh
            parts.append(
                f'SELECT fv.cell, COALESCE(nn.canonical, fv.cell) '
                f'FROM (VALUES {vlits}) AS fv(cell, vec) '
                f'LEFT JOIN LATERAL (SELECT w.canonical FROM world."words" w '
                f'WHERE w.type={qlit(wtype)}{ctx_sql} AND (w.embedding <=> fv.vec) <= {maxd} '
                f'ORDER BY w.embedding <=> fv.vec LIMIT 1) nn ON true')
        return " UNION ALL ".join(parts) if parts else None

    def _city_bridge_sql(self, norm, mtab, route_col, ctx_country):
        """Resolve each uploaded city cell to a SPECIFIC city qid. Exact name/altLabel candidates are ranked
        (context country -> global is_primary -> population) in Python — deterministic, not a similarity search;
        the TYPO remainder is a tight in-SQL <=> nearest-neighbour (FUZZY_THRESH), context-constrained. -> SQL
        yielding (cell, qid), or None. A cell matching no city resolves to no row (excluded), like a failed join.
        'Bombay' under 'in India' -> Mumbai's qid (altLabel + context); 'Mumbai' under 'in US' -> Mumbai-India's
        qid (no US candidate) which the downstream country filter then excludes; 'London' under 'in US' -> the
        highest-population US London."""
        t = next((x for x in norm if x["name"] == mtab), None)
        if not t or route_col not in t["columns"]:
            return None
        ci = t["columns"].index(route_col)
        cells = list({str(r[ci]) for r in t["rows"] if r[ci] not in (None, "")})
        if not cells:
            return None
        cur = self._rconn().cursor()
        norms = {c: normalize_surface(c) for c in cells}
        uniq = sorted({n for n in norms.values() if n})
        cand = {}                                                 # norm -> [(qid, country, is_primary, population)]
        if uniq:
            cur.execute("SELECT norm, qid, canon_country, is_primary, (props->>'population')::bigint "
                        "FROM world.\"words\" WHERE type='city' AND qid IS NOT NULL AND norm = ANY(%s)", (uniq,))
            for nm, qid, co, prim, pop in cur.fetchall():
                cand.setdefault(nm, []).append((qid, co, bool(prim), pop or 0))

        def pick(c):
            cs = cand.get(norms[c])                               # context country, then global is_primary, then pop
            return sorted(cs, key=lambda x: ((x[1] == ctx_country) if ctx_country else False, x[2], x[3]),
                          reverse=True)[0][0] if cs else None
        exact_rows, fuzzy = [], []
        for c in cells:
            q = pick(c)
            (exact_rows.append((c, q)) if q else fuzzy.append(c))
        if exact_rows:                                            # LAZY-SYNC the resolved cities into wikipedia."city"
            try:                                                  # (tables start empty; fill the qids we just resolved)
                from engine.world_sync import ensure_entity
                qids = [q for _c, q in exact_rows]
                for q in qids:
                    ensure_entity(q, "Q515")                      # city type = Q515 -> wikipedia."city" (qid PK + qid FKs)
                cur.execute('SELECT DISTINCT country FROM wikipedia."city" WHERE qid = ANY(%s) AND country IS NOT NULL', (qids,))
                for (cq,) in cur.fetchall():                      # cascade the FK: fill each city's COUNTRY so 2-hop
                    ensure_entity(cq, "Q6256")                    # filters (continent) can join wikipedia."country"
            except Exception as e:                                # noqa: BLE001 — never block the query on a sync miss
                print(f"[entities] city lazy-fill failed: {e}", flush=True)
        parts = []
        if exact_rows:
            parts.append("SELECT * FROM (VALUES " +
                         ", ".join(f"({qlit(c)}, {qlit(q)})" for c, q in exact_rows) + ") AS ex(cell, qid)")
        if fuzzy:                                                 # genuine typos only — tight, context-constrained
            vecs = Embedder.get().encode(fuzzy)
            vlits = ", ".join(f"({qlit(c)}, {qlit(pgvector_literal(v))}::vector)" for c, v in zip(fuzzy, vecs))
            maxd = 1.0 - self.FUZZY_THRESH
            ctx_sql = f" AND w.canon_country = {qlit(ctx_country)}" if ctx_country else ""
            parts.append(
                f'SELECT fv.cell, nn.qid FROM (VALUES {vlits}) AS fv(cell, vec) '
                f'JOIN LATERAL (SELECT w.qid FROM world."words" w WHERE w.type=\'city\' AND w.qid IS NOT NULL'
                f'{ctx_sql} AND (w.embedding <=> fv.vec) <= {maxd} ORDER BY w.embedding <=> fv.vec LIMIT 1) nn ON true')
        return " UNION ALL ".join(parts) if parts else None

    def _city_bridge_disamb_sql(self, norm, mtab, route_col, disamb_col):
        """Same-name city disambiguation when the UPLOAD itself carries a per-row country column (branches_geo has
        BOTH a city column and a country column to separate its two 'Paris' rows). Resolve each (city, country) PAIR
        to the city qid whose canon_country matches THAT row's country (then is_primary, population), and yield
        (cell, ctx, qid) so the join pins BOTH the city name AND its row-country. This is the qid-keyed replacement
        for the old name-vs-name disambiguator join: wikipedia."city".country is a country QID ('Q142'), not 'France',
        so the inherited lower(up.country)=lower(city.country) never matched -> empty SUM. -> SQL | None."""
        t = next((x for x in norm if x["name"] == mtab), None)
        if not t or route_col not in t["columns"] or disamb_col not in t["columns"]:
            return None
        ci = t["columns"].index(route_col); di = t["columns"].index(disamb_col)
        pairs = sorted({(str(r[ci]), str(r[di])) for r in t["rows"]
                        if r[ci] not in (None, "") and r[di] not in (None, "")})
        if not pairs:
            return None
        cur = self._rconn().cursor()
        cnorm = {c: normalize_surface(c) for c, _ in pairs}
        uniq = sorted({n for n in cnorm.values() if n})
        cand = {}                                                 # norm -> [(qid, canon_country, is_primary, pop)]
        if uniq:
            cur.execute("SELECT norm, qid, canon_country, is_primary, (props->>'population')::bigint "
                        "FROM world.\"words\" WHERE type='city' AND qid IS NOT NULL AND norm = ANY(%s)", (uniq,))
            for nm, qid, co, prim, pop in cur.fetchall():
                cand.setdefault(nm, []).append((qid, (co or ""), bool(prim), pop or 0))
        rows = []
        for cell, ctx in pairs:                                   # pick the qid whose canon_country == this row's country
            cs = cand.get(cnorm[cell])
            if not cs:
                continue
            q = sorted(cs, key=lambda x: (x[1].lower() == ctx.lower(), x[2], x[3]), reverse=True)[0][0]
            rows.append((cell, ctx, q))
        if not rows:
            return None
        try:                                                      # LAZY-SYNC the resolved cities + their countries
            from engine.world_sync import ensure_entity           # (qid PK + country qid FK) so 2-hop joins hit
            qids = [q for _c, _x, q in rows]
            for q in qids:
                ensure_entity(q, "Q515")
            cur.execute('SELECT DISTINCT country FROM wikipedia."city" WHERE qid = ANY(%s) AND country IS NOT NULL', (qids,))
            for (cq,) in cur.fetchall():
                ensure_entity(cq, "Q6256")
        except Exception as e:                                    # noqa: BLE001 — never block the query on a sync miss
            print(f"[entities] disamb city lazy-fill failed: {e}", flush=True)
        vals = ", ".join(f"({qlit(c)}, {qlit(x)}, {qlit(q)})" for c, x, q in rows)
        return f"SELECT * FROM (VALUES {vals}) AS ex(cell, ctx, qid)"

    def _labelize_qids(self, result):
        """Resolve entity QIDs in the FIRST projected column to canonical labels via world."words" (the qid->canonical
        index; world."types" holds only the taxonomy, not entity labels). So a projected world entity-attribute column
        ('which continent is Kyoto in' -> country.continent = 'Q48') reads 'Asia', not the bare qid. Non-qid values
        (a plain text projection) pass through unchanged."""
        import re as _re
        rows = result.get("rows") or []
        qids = sorted({str(r[0]) for r in rows if r and _re.fullmatch(r"Q\d+", str(r[0]))})
        if not qids:
            return
        try:
            cur = self._rconn().cursor()
            cur.execute('SELECT qid, canonical FROM world."words" WHERE qid = ANY(%s) AND canonical IS NOT NULL', (qids,))
            lbl = {q: c for q, c in cur.fetchall()}
        except Exception as e:                                    # noqa: BLE001 — leave qids as-is on a lookup miss
            print(f"[entities] qid->label resolve failed: {e}", flush=True); return
        if lbl:
            result["rows"] = [([lbl.get(str(r[0]), r[0])] + list(r[1:])) if r else r for r in rows]

    def _world_joins(self, upfrom, joins, sch, norm, mtab, route_col, as_of, mf=None):
        """idx==0 = the CELL-side join. CITY cells resolve to a stable qid (context-aware, robust same-name
        disambiguation) and join on Cities.qid; other world types use the name bridge (safe superset of
        lower()=lower()). When the query filters to a country, that country is the resolution CONTEXT."""
        fw = upfrom
        wt0 = joins[0]["right_table"]
        disamb = self.disambiguator(sch, mtab, route_col, wt0)
        warnings = [] if disamb else self.ambiguities(next(t for t in norm if t["name"] == mtab), route_col, wt0)
        wtype = WORLD_TABLE_TYPE.get(wt0)
        ctx_country = mf.get("value") if (mf and mf.get("attr") == "country") else None
        city_bridge = self._city_bridge_sql(norm, mtab, route_col, ctx_country) if (wtype == "city" and not disamb) else None
        # disamb + city: the upload carries a per-row country column to separate same-name cities (branches_geo's two
        # 'Paris' rows). The inherited name-vs-name disambiguator join can't work qid-keyed, so resolve each
        # (city, country) PAIR to a qid and join on BOTH -> the row-correct city (replaces the broken else-branch).
        disamb_city_bridge = (self._city_bridge_disamb_sql(norm, mtab, route_col, disamb[0])
                              if (wtype == "city" and disamb) else None)
        name_bridge = (self._cell_bridge_sql(norm, mtab, route_col, wtype)
                       if (wtype and wtype != "city" and not disamb) else None)
        for idx, j in enumerate(joins):
            R = j["right_table"]
            if idx == 0 and disamb_city_bridge:
                b = "__bridge0"
                fw += (f' JOIN ({disamb_city_bridge}) AS {b}(cell, ctx, qid)'
                       f' ON {b}.cell = {qident(mtab)}.{qident(route_col)}'
                       f' AND {b}.ctx = {qident(mtab)}.{qident(disamb[0])}')
                fw += f' JOIN {qident(R)} ON {qident(R)}.{qident("qid")} = {b}.qid'   # qid pins the row-disambiguated city
            elif idx == 0 and city_bridge:
                b = "__bridge0"
                fw += f' JOIN ({city_bridge}) AS {b}(cell, qid) ON {b}.cell = {qident(mtab)}.{qident(route_col)}'
                fw += f' JOIN {qident(R)} ON {qident(R)}.{qident("qid")} = {b}.qid'   # qid pins the exact city
            elif idx == 0 and name_bridge:
                b = "__bridge0"
                fw += f' JOIN ({name_bridge}) AS {b}(cell, canon) ON {b}.cell = {qident(mtab)}.{qident(route_col)}'
                cond = f'lower({qident(R)}.{qident(j["right_col"])}) = lower({b}.canon)'
                if "is_primary" in self.words[R].get("columns", []):
                    cond += f' AND {qident(R)}.{qident("is_primary")} = 1'
                if "valid_from" in self.words[R]["columns"]:
                    cond += (f' AND {qident(R)}.{qident("valid_from")} <= {qlit(as_of)}'
                             f' AND ({qident(R)}.{qident("valid_to")} IS NULL OR {qlit(as_of)} < {qident(R)}.{qident("valid_to")})')
                fw += f' JOIN {qident(R)} ON {cond}'
            else:
                fw += f' JOIN {qident(R)} ON {self._join_cond(idx, j, mtab, disamb, as_of)}'
        return fw, disamb, warnings
