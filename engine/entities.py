"""Entity resolution by EMBEDDING NEAREST-NEIGHBOUR, replacing hardcoded alias tables and every lower()
string hack.

A filter value that is ABSENT from the upload ("cities in US") is resolved like this:
  spaCy parses the prompt -> candidate surface forms ("US")
  bge-small embeds each candidate
  cosine NN against the world `words` vector index, RESTRICTED to the filter attr's entity type (country)
  if the best match clears a calibrated threshold -> use that canonical word ("United States") in the WHERE
No alias list, no lower(), no string matching. "US"/"USA"/"UK"/"Deutschland" resolve by meaning; the preposition
"in" and non-entities ("Indiana") stay below threshold and fall through.

This overrides ONLY value resolution (KnowledgeTableQuery._find_value) plus the cell-side bridge (uploaded city
values -> world PK via a per-upload bridge). Everything else — routing, joins, SQL assembly, multi-table FK,
the analyze view — is inherited unchanged from the layers below.

Cells are matched to entities once per version of their table (engine/prepared.py): ``_city_matches`` and
``_name_matches`` read the knowledgebase then, and the sheet alone decides which entity a cell names
(``_city_pairs``). A question reads its own words through the instance's cached knowledge.
"""
from __future__ import annotations
import re
import time

import psycopg2

from engine import request_timing
from engine.config import DATA_DIR
from engine.resolve_base import RoutedQuery
from engine.pg import _pg
from engine.request_state import RequestLocal
from engine.knowledge_tables import qident, qlit
from engine.dataset_semantics import is_synthetic_currency_column
from engine.embeddings import Embedder, demonym_stems, pgvector_literal, normalize_surface
from engine.prepared import current_database, knowledge

# filter attr (as named in word_*.json) -> the entity `type` it is matched against in knowledgebase."words"
ENTITY_TYPES = {"country": "country", "nation": "country", "continent": "continent",
                "state": "state", "american_state": "state", "province": "state", "region": "state",
                "element": "element", "city": "city", "municipality": "city"}
PLACE_TYPES = frozenset(ENTITY_TYPES.values()) - {"element"}   # the entity types that name a place
# Closed-class function words that are never an entity — dropped as candidates BEFORE the NN lookup.
# This is a correctness guard, not an optimization: the embedding NN happily clears the threshold on
# them ("from" -> Belarus at 0.82 turned "orders from Paris" into a Belarus filter and a wrong 0),
# and a closed-class word can never be the value the question filters by.
STOP = {"in", "the", "of", "a", "an", "for", "to", "by", "on", "at", "with", "and", "or", "is", "are",
        "how", "many", "much", "total", "sum", "average", "count", "number", "all", "each", "per", "their",
        "from", "into", "onto", "over", "under", "between", "during", "since", "until", "about", "across",
        "after", "before", "above", "below", "against", "through", "within", "without",
        "where", "which", "who", "whom", "whose", "what", "when", "why",
        "was", "were", "be", "been", "does", "did", "do", "has", "have", "had",
        "will", "would", "can", "could", "should", "shall", "may", "might",
        "that", "this", "these", "those", "there", "than", "as", "if", "not", "no", "but", "it", "its"}
# QID-keyed table (exact Wikidata label) -> the `type` its rows carry in knowledgebase."words" (cell-side bridge resolution).
# The tables are the qid-keyed knowledgebase."city"/"country" (via WORLD_NAMES); the planner resolves both the cell
# bridge and the filter to QIDS, so every world join + filter is qid PK/FK.
# Route value -> the `type` its cells carry in knowledgebase."words" (for cell-side bridge resolution).
# city/country use the qid-keyed knowledgebase."<type>" tables; u_s_state uses the aggregate qid-keyed
# knowledgebase."u_s_state" (its cells resolve as type='state'; see db/sync/build_u_s_state.py). element/etc. remain
# on the friendly name-keyed family (routed by the value-membership fallback). See docs/notes/naming.md.
# Route values are the logical world-table names emitted by ``resolve_base``.  Keep
# the element mapping here as well as the geo mappings because the value-membership
# fallback is the source-grounded path used when the classifier abstains.
WORLD_TABLE_TYPE = {"city": "city", "country": "country", "u_s_state": "state",
                    "Elements in the World": "element"}
TYPE_TO_FRIENDLY = {v: k for k, v in WORLD_TABLE_TYPE.items()}   # state -> u_s_state; element -> friendly view
VALUE_ROUTE_MIN = 0.80   # a column routes to a world table when >= this fraction of its cells resolve to that type
# A request's resolution connection is checked with one round trip before reuse once it has been idle
# this long. The managed Cloud SQL connection drops idle connections: all five production failures
# (`world request failed: OperationalError`, 2026-08-30..09-25) came 5-57 s after the connector
# refreshed its certificate (cloudsql.instances.connect), 8 s to 41 min after the connection's last
# successful use, when one connection served every request. psycopg2 reports a connection closed only
# after a statement fails on it. A request idle for longer than this (reading a question, a long pool
# run) is checked, while statements milliseconds apart are not.
RCONN_IDLE_CHECK_S = 1.0


class EntityQuery(RoutedQuery):
    """RoutedQuery + embedding-NN value resolution (bge-small over the world `words` pgvector index)."""

    THRESH = 0.80            # prompt-side fuzzy-fallback floor; exact altLabel match handles conventional aliases
    FUZZY_THRESH = 0.88      # cell-side TYPO floor (tight): aliases come from altLabel DATA, not loose fuzzy

    # Per request, as concurrent questions share this object (engine/request_state.py): the request's
    # resolution connection, when _rconn last handed it out (the idle clock for its liveness check), its
    # shared-knowledge memo and its question as typed.
    _rcn = RequestLocal()
    _rcn_used = RequestLocal()
    _kb_memo = RequestLocal()
    _q_orig = RequestLocal()

    def __init__(self, deploy_dir=DATA_DIR):
        super().__init__(deploy_dir)
        self._nlp = None

    # ---- helpers ----
    def _spacy(self):
        if self._nlp is None:
            from engine.closed_class import spacy_model
            self._nlp = spacy_model()                    # the process's one spaCy model
        return self._nlp

    def _rconn(self):
        # The connection a table's preparation reads the knowledgebase on (engine/prepared.py). AUTOCOMMIT: it issues
        # many independent read SELECTs, and an implicit transaction left "idle in transaction" would hold read locks
        # on knowledgebase."words" for as long as the preparation runs.
        now = time.monotonic()
        last_used = getattr(self, "_rcn_used", None)
        if (self._rcn is not None and not self._rcn.closed and last_used is not None
                and now - last_used > RCONN_IDLE_CHECK_S):
            try:
                with self._rcn.cursor() as cur:
                    cur.execute("SELECT 1")
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                self._rcn.close()                                # dropped between requests
                with request_timing.span("pg_stale_reconnect"):  # the request's [timing] line shows it
                    self._open_rconn()
        if self._rcn is None or self._rcn.closed:
            self._open_rconn()
        self._rcn_used = now
        return self._rcn

    def _open_rconn(self):
        self._rcn = _pg()
        self._rcn.autocommit = True

    def end_request(self):
        """Close the resolution connection this request opened."""
        connection, self._rcn = self._rcn, None
        if connection is not None and not connection.closed:
            connection.close()

    # ---- request-scoped memo for shared-knowledge reads ----
    def begin_request(self):
        """Open a fresh request-scoped memo for `_kb_rows`. Called once per serve request by the ONE
        production entry (engine.knowledge.KnowledgeReasoner.serve), so identical lookups within a
        request — the routing pass and the delegate pass both resolving the same columns, the non-geo
        scan re-classifying the same cells — hit Postgres once. Measured before this existed: the
        three hot `words` lookup shapes ran 2-4x each per request with identical parameters, 5.8s of
        a 6.6s server-side total. The memo holds only read results from shared knowledgebase tables,
        which change solely via offline sync — never uploaded data or per-conversation state — so
        within one request a repeat is exact."""
        self._kb_memo = {}

    def _kb_rows(self, sql, params=()):
        """Execute a read-only shared-knowledge lookup, memoized for the current request.

        Keyed on the full (sql, params) tuple: only a byte-identical repeat reuses rows, so this
        cannot change any answer. Outside a request (no `begin_request`) it is an uncached
        passthrough. Do NOT route conversation-schema or bridge reads through this — those tables
        are written mid-request."""
        memo = getattr(self, "_kb_memo", None)
        key = (sql, tuple(tuple(p) if isinstance(p, list) else p for p in params))
        if memo is not None and key in memo:
            return memo[key]
        cur = self._rconn().cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        if memo is not None:
            memo[key] = rows
        return rows

    def _names_by_type(self, norms):
        """{norm: {type: {qid}}} for the normalized names in ``norms`` that name a world entity exactly, in
        one lookup. The resolver's exact pass and the coverage gate's place check both read names here; the
        instance keeps each name it read (engine.prepared.Knowledge.names_by_type)."""
        return knowledge().names_by_type(norms)

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
        """(qid, cosine) of the ``type_`` entity whose name is nearest ``vec``. A country, continent or state is
        searched exactly among every name of its type, in memory; a city among the cities the request's tables
        name: a city the sheets never mention matches none of their rows. Each was an approximate PostgreSQL
        search before, 0.5 to 0.9 s for a city (2026-10-27)."""
        if type_ == "city":
            database = current_database()
            return database.nearest_city(vec) if database is not None else (None, -1.0)
        return knowledge().nearest(vec, type_)

    @staticmethod
    def _type_forms(candidate):
        """The normalized singular forms under which a candidate could NAME a world type: its head (last)
        word ('which countries' -> country) and the whole phrase ('human settlements' -> humansettlement)."""
        words = candidate.lower().split()
        if not words:
            return set()
        head = words[-1]
        singulars = {head}
        if head.endswith("s"):
            singulars.add(head[:-1])
        if head.endswith("es"):
            singulars.add(head[:-2])
        if head.endswith("ies"):
            singulars.add(head[:-3] + "y")
        forms = {normalize_surface(word) for word in singulars}
        forms |= {normalize_surface(" ".join(words[:-1] + [word])) for word in singulars}
        return {form for form in forms if form}

    def _resolve(self, question, type_, exact_only=False):
        """resolve `question` to a canonical world PK of `type_`. HYBRID:
        (1) normalized-EXACT match of a candidate surface form against the words index (deterministic — this is
            what nails US/USA/UK/Holland/the-United-States, which embeddings get wrong); longest candidate first,
            and a norm that maps to >1 canonical is skipped as ambiguous;
        (2) embedding NN fallback for anything not matched exactly (typos / novel forms), gated by THRESH.
        Returns (canonical, score, surface) or None."""
        cands = self._candidates(question)
        if not cands:
            return None
        norms = {c: normalize_surface(c) for c in cands}
        type_forms = {c: self._type_forms(c) for c in cands}
        # A continent's demonym is its name plus '-n'/'-an' ('European', 'North American'), read EXACTLY: the
        # embedding put 'North American' nearer the United States, so 'orders from North American countries'
        # filtered one country (2026-09-30). Only for continents, where the stem must itself be a continent;
        # for every type, 'plan' would read as 'pl' and 'can' as 'ca'.
        stems = {c: [normalize_surface(stem) for stem in demonym_stems(c)]
                 for c in cands} if type_ == "continent" else {}
        by_type = self._names_by_type(                           # norm -> {type: {qid}} across ALL types
            {n for n in norms.values() if n} | {f for forms in type_forms.values() for f in forms}
            | {stem for group in stems.values() for stem in group if stem})
        for c in sorted(cands, key=lambda x: -len(x)):           # (1) exact match of the requested type, longest first
            cs = by_type.get(norms[c], {}).get(type_)
            for stem in ([] if cs else stems.get(c, ())):
                cs = by_type.get(stem, {}).get(type_)
                if cs:
                    break
            if cs and len(cs) == 1:
                return (next(iter(cs)), 1.0, c)
        if exact_only:
            return None
        # (2) fuzzy fallback — ONLY for candidates that exact-match NOTHING. A token that IS a known state/city/etc
        # (Indiana, Houston) is that entity, not a typo of a country, so it must not fuzzy-match a country. Nor
        # may a candidate that NAMES a world type ('countries', 'which countries'): it is the type, never one of
        # its members. The embedding put 'countries' 0.803 from China and 'which countries' 0.807 from the
        # United Kingdom, over THRESH, so 'how many countries are the customers in' filtered China and answered
        # 0 (2026-09-28). Exact names still win above ('Mexico City', 'New York State').
        fuzzy = [c for c in cands if not by_type.get(norms[c])
                 and not any("type" in by_type.get(form, {}) for form in type_forms[c])]
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
    def serve(self, tables, question, as_of=None, schema=None, explicit_fks=(), dataset_semantics=()):
        self._q_orig = question                      # original-case prompt for spaCy + bge (low_q is lowercased)
        return super().serve(tables, question, as_of=as_of,
                             explicit_fks=explicit_fks, dataset_semantics=dataset_semantics)

    def _find_value(self, low_q, w, exact_only=False):
        """resolve a where value via embedding NN over the world `words` index for any ENTITY-typed filter attr;
        fall back to the plain matcher (bypassing the alias table) for non-entity attrs. ``exact_only`` admits
        exact names alone (meaning_filter's first pass)."""
        # _q_meaning = the question with spans already CLAIMED by another reading removed (the
        # conversion phrase: "in US dollars" must not also resolve as the country United States).
        q = getattr(self, "_q_meaning", None) or getattr(self, "_q_orig", None) or low_q
        best = None                                  # (attr, canonical, sim)
        for attr in w.get("filter_attrs", []):
            t = ENTITY_TYPES.get(attr)
            if not t:
                continue
            r = self._resolve(q, t, exact_only=exact_only)
            if r and (best is None or r[1] > best[2]):
                best = (attr, r[0], r[1])
        if best:
            return best[0], best[1]
        return super(RoutedQuery, self)._find_value(low_q, w, exact_only=exact_only)   # skip the alias table -> plain match

    # ---- routing: deterministic VALUE-MEMBERSHIP (header-independent) over the model fallback ----
    def _value_membership_routes(self, table):
        """A column routes to the world table whose `type` >= VALUE_ROUTE_MIN of its cell VALUES resolve to in the
        `words` index (exact normalized membership) — regardless of the header ('Countries' / 'Country code' /
        'Nation' all route by their values). Column-level aggregation averages out single-value collisions
        (IN = India vs the state Indiana). -> {(table, col): friendly_world_table}."""
        base = table["name"]
        routes = {}
        columns = []                                              # (col, cells, norms) of every candidate column
        for ci, col in enumerate(table["columns"]):
            # A synthesized measure-currency column is ENGINE METADATA, not user data: every cell is
            # the same ISO code. Value-membership resolution happily matched 'EUR' to a city QID and
            # built a world join on it, so "total budget in Germany in US dollars" filtered
            # city.country = Germany against a city in Italy, matched nothing, and returned an empty
            # SUM presented as an answer (2026-09-08). Internal columns are never world entities.
            if is_synthetic_currency_column(col):
                continue
            cells = [str(r[ci]) for r in table["rows"] if ci < len(r) and r[ci] not in (None, "")]
            if len(cells) < 3:
                continue
            norms = [normalize_surface(c) for c in cells]
            if any(norms):
                columns.append((col, cells, norms))
        # ONE membership lookup for every column's values: it was one round trip per column (seven for the
        # customer-orders sheet) on every world request, 2026-09-30. A number names no place: dates, amounts
        # and numeric ids were most of the ~100,000 values an 11,500-row subscriptions tab sent, and the
        # lookup took 5 s (2026-10-04). Four Wikidata place names are digits (area codes such as "416"); a
        # column of them is not a column of places.
        uniq = sorted({n for _col, _cells, norms in columns for n in norms if n and not n.isdigit()})
        ntypes = {}
        if uniq:
            for nm, ty in self._kb_rows(
                    'SELECT DISTINCT norm, type FROM knowledgebase."words" WHERE norm = ANY(%s) '
                    "AND type IN ('city','country','state','element','continent')", (uniq,)):
                ntypes.setdefault(nm, set()).add(ty)
        for col, cells, norms in columns:
            counts = {}
            for n in norms:
                for ty in ntypes.get(n, ()):                      # a cell may match >1 type (Georgia); count each
                    counts[ty] = counts.get(ty, 0) + 1
            if not counts:
                continue
            best_ty, best = max(sorted(counts.items()), key=lambda kv: kv[1])   # a tie takes the first type by name
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
    # Preparation reads the knowledgebase once per table version (engine/prepared.py); a question picks.

    @staticmethod
    def _cells(table, column):
        """The distinct non-empty cells of ``column``, as text."""
        index = table["columns"].index(column)
        return sorted({str(row[index]) for row in table["rows"]
                       if index < len(row) and row[index] not in (None, "")})

    def _city_matches(self, cells):
        """Every city each cell can name: {cell: [[qid, country name, is_primary, population, sim], ...]}. A cell
        whose normalized name is a city's name or altLabel lists those cities (sim 1.0); a typo lists the cities
        within FUZZY_THRESH of it, nearest first (a nearest-neighbour search over the city names). ``_city_pairs``
        picks one. Read at preparation."""
        cur = self._rconn().cursor()
        norms = {c: normalize_surface(c) for c in cells}
        uniq = sorted({n for n in norms.values() if n})
        exact = {}                                                # norm -> [(qid, country, is_primary, population)]
        if uniq:
            cur.execute("SELECT norm, qid, canon_country, is_primary, (props->>'population')::bigint "
                        "FROM knowledgebase.\"words\" WHERE type='city' AND qid IS NOT NULL AND norm = ANY(%s) "
                        "ORDER BY norm, qid, id", (uniq,))
            for nm, qid, co, prim, pop in cur.fetchall():
                exact.setdefault(nm, []).append([qid, co, bool(prim), int(pop or 0), 1.0])
        out = {cell: exact[norms[cell]] for cell in cells if exact.get(norms[cell])}
        fuzzy = [cell for cell in cells if cell not in out]
        if fuzzy:                                                 # genuine typos only — tight
            vecs = Embedder.get().encode(fuzzy)
            maxd = 1.0 - self.FUZZY_THRESH
            for start in range(0, len(fuzzy), 200):
                part = list(zip(fuzzy[start:start + 200], vecs[start:start + 200]))
                vlits = ", ".join(f"({qlit(c)}, {qlit(pgvector_literal(v))}::vector)" for c, v in part)
                cur.execute(
                    f'SELECT fv.cell, nn.qid, nn.country, nn.is_primary, nn.population, nn.sim '
                    f'FROM (VALUES {vlits}) AS fv(cell, vec) JOIN LATERAL ('
                    f"SELECT w.qid, w.canon_country AS country, w.is_primary, (w.props->>'population')::bigint "
                    f'AS population, 1 - (w.embedding <=> fv.vec) AS sim FROM knowledgebase."words" w '
                    f"WHERE w.type='city' AND w.qid IS NOT NULL AND (w.embedding <=> fv.vec) <= {maxd} "
                    f'ORDER BY w.embedding <=> fv.vec, w.qid LIMIT 10) nn ON true')
                for cell, qid, co, prim, pop, sim in cur.fetchall():
                    out.setdefault(cell, []).append([qid, co, bool(prim), int(pop or 0), float(sim)])
        return out

    def _name_matches(self, cells, wtype):
        """The entity of ``wtype`` each cell names: {cell: canonical name}. An exact normalized name that names one
        entity, else the nearest name within THRESH, else none. Read at preparation."""
        cur = self._rconn().cursor()
        norms = {c: normalize_surface(c) for c in cells}
        uniq = sorted({n for n in norms.values() if n})
        exact = {}
        if uniq:                                                  # one batch query, NOT per-cell
            cur.execute('SELECT w.norm, w.canonical FROM knowledgebase."words" w WHERE w.type=%s AND w.norm = ANY(%s)',
                        (wtype, uniq))
            tmp = {}
            for nm, cn_ in cur.fetchall():
                tmp.setdefault(nm, set()).add(cn_)
            exact = {nm: next(iter(cs)) for nm, cs in tmp.items() if len(cs) == 1}   # unique exact only
        out = {cell: exact[norms[cell]] for cell in cells if norms[cell] in exact}
        fuzzy = [cell for cell in cells if cell not in out]
        if fuzzy:                                                 # similarity search runs IN POSTGRES (LATERAL <=>)
            vecs = Embedder.get().encode(fuzzy)
            maxd = 1.0 - self.THRESH                              # cosine distance <= 1-thresh  <=>  cosine sim >= thresh
            for start in range(0, len(fuzzy), 200):
                part = list(zip(fuzzy[start:start + 200], vecs[start:start + 200]))
                vlits = ", ".join(f"({qlit(c)}, {qlit(pgvector_literal(v))}::vector)" for c, v in part)
                cur.execute(
                    f'SELECT fv.cell, nn.canonical FROM (VALUES {vlits}) AS fv(cell, vec) '
                    f'JOIN LATERAL (SELECT w.canonical FROM knowledgebase."words" w '
                    f'WHERE w.type={qlit(wtype)} AND (w.embedding <=> fv.vec) <= {maxd} '
                    f'ORDER BY w.embedding <=> fv.vec LIMIT 1) nn ON true')
                out.update((cell, canonical) for cell, canonical in cur.fetchall() if canonical)
        return out

    def _city_pairs(self, norm, mtab, route_col):
        """Each uploaded city cell's city: [(cell, qid)]. An exact name (or altLabel: 'Bombay' -> Mumbai) takes the
        name's primary city, then the most populous; a typo its nearest city. A cell matching no city has no pair
        (excluded, like a failed join). A sheet with a country column separates its namesakes row by row
        (``_city_disamb_pairs``).

        The question never chooses. A country it names filters the rows; it is no evidence of which city a cell
        names, and reading 'London' as London, Kentucky under 'in the United States' would let every namesake pass
        the filter (tests.test_question_families: "total amount in the United States in euros")."""
        t = next((x for x in norm if x["name"] == mtab), None)
        if not t or route_col not in t["columns"]:
            return []
        matches = self._prepared(t).columns.get(route_col, {}).get("cells", {})
        pairs = []
        for cell in self._cells(t, route_col):
            candidates = matches.get(cell) or []
            exact = [c for c in candidates if c[4] >= 1.0]
            ranked = sorted(exact, key=lambda c: (c[2], c[3]), reverse=True) if exact else candidates
            if ranked:
                pairs.append((cell, ranked[0][0]))
        return pairs

    def _name_pairs(self, norm, mtab, route_col):
        """Each uploaded cell's entity of the column's world type: [(cell, canonical name)]; a cell matching none
        keeps its own text (the old lower()=lower() join)."""
        t = next((x for x in norm if x["name"] == mtab), None)
        if not t or route_col not in t["columns"]:
            return []
        matches = self._prepared(t).columns.get(route_col, {}).get("cells", {})
        return [(cell, matches.get(cell) or cell) for cell in self._cells(t, route_col)]

    def _city_disamb_pairs(self, norm, mtab, route_col, disamb_col):
        """Same-name city disambiguation when the UPLOAD itself carries a per-row country column (branches_geo has
        BOTH a city column and a country column to separate its two 'Paris' rows). Each (city, country) PAIR takes
        the city whose country name is THAT row's country (then is_primary, population): [((cell, country), qid)],
        so the join pins BOTH the city name AND its row-country. knowledgebase."city".country is a country QID
        ('Q142'), not 'France', so a name-vs-name join on the city table never matched -> empty SUM."""
        t = next((x for x in norm if x["name"] == mtab), None)
        if not t or route_col not in t["columns"] or disamb_col not in t["columns"]:
            return []
        ci = t["columns"].index(route_col); di = t["columns"].index(disamb_col)
        rows = sorted({(str(r[ci]), str(r[di])) for r in t["rows"]
                       if r[ci] not in (None, "") and r[di] not in (None, "")})
        matches = self._prepared(t).columns.get(route_col, {}).get("cells", {})
        pairs = []
        for cell, ctx in rows:                                    # the qid whose country name == this row's country
            exact = [c for c in (matches.get(cell) or []) if c[4] >= 1.0]
            if exact:
                best = sorted(exact, key=lambda c: ((c[1] or "").lower() == ctx.lower(), c[2], c[3]),
                              reverse=True)[0]
                pairs.append(((cell, ctx), best[0]))
        return pairs

    def _qid_labels(self, qids):
        """qid -> canonical label (the qid->canonical index of knowledgebase."words"; knowledgebase."types" holds
        only the taxonomy, not entity labels). Returns {} on a lookup miss so callers degrade to showing the qid.

        A qid can carry more than one canonical row: a later sync added "Czech Republic" beside the source's
        "Czechia" (Q213) and "People's Republic of China" beside "China" (Q148). The label is the primary row's,
        then the one the source record names itself, then the earliest: building the map from every row let
        the row order decide, and the world answer said "Czech Republic" where compose said "Czechia"
        (2026-10-02). This is the one qid label source (engine.prepared.read_labels); every trail and answer reads
        it, through the request's database and the instance's cache."""
        try:
            return knowledge().labels(qids, current_database())
        except Exception as e:                                    # noqa: BLE001 — leave qids as-is on a lookup miss
            print(f"[entities] qid_to_label_failed error={type(e).__name__}", flush=True)
            return {}

    def _world_joins(self, upfrom, joins, sch, norm, mtab, route_col, as_of, mf=None):
        """idx==0 = the CELL-side join. CITY cells resolve to a stable qid (``_city_pairs``; a country column in
        the sheet separates same-name cities) and join on Cities.qid; other world types use the name bridge (safe
        superset of lower()=lower()). ``mf``, the question's world filter, is applied after the join; it never
        changes which entity a cell names."""
        fw = upfrom
        wt0 = joins[0]["right_table"]
        disamb = self.disambiguator(sch, mtab, route_col, wt0)
        warnings = [] if disamb else self.ambiguities(next(t for t in norm if t["name"] == mtab), route_col, wt0)
        wtype = WORLD_TABLE_TYPE.get(wt0)
        city_bridge = self._city_bridge_sql(norm, mtab, route_col) if (wtype == "city" and not disamb) else None
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
                rows = self._bridge_select(mtab, route_col, ("value", "context", "entity_qid"), ("cell", "ctx", "qid"))
                fw += (f' JOIN ({rows}) AS {b}'
                       f' ON {b}.cell = {qident(mtab)}.{qident(route_col)}'
                       f' AND {b}.ctx = {qident(mtab)}.{qident(disamb[0])}')
                fw += f' JOIN {qident(R)} ON {qident(R)}.{qident("qid")} = {b}.qid'   # qid pins the row-disambiguated city
            elif idx == 0 and city_bridge:
                b = "__bridge0"
                rows = self._bridge_select(mtab, route_col, ("value", "world_key"), ("cell", "qid"))
                fw += f' JOIN ({rows}) AS {b} ON {b}.cell = {qident(mtab)}.{qident(route_col)}'
                fw += f' JOIN {qident(R)} ON {qident(R)}.{qident("qid")} = {b}.qid'   # qid pins the exact city
            elif idx == 0 and name_bridge:
                b = "__bridge0"
                if j["right_col"] == "qid":
                    # QID-KEYED world table (knowledgebase."country" migrated to qid keys): the name bridge yields the
                    # canonical NAME, but the table joins on `qid`, so a name/qid join returned 0 rows — every
                    # country-column world join was empty. The bridge's entity_qid is the ONE qid the canonical name
                    # names (the lowest of its qids, KnowledgeBridgeMixin._bridge_rows), so the aggregate cannot fan out
                    # by the name's altLabel count. Robust to official-vs-common name mismatch (China's canon 'China'
                    # -> Q148 -> knowledgebase.country.qid Q148), which a name-join silently drops.
                    rows = self._bridge_select(mtab, route_col, ("value", "entity_qid"), ("cell", "qid"))
                    fw += f' JOIN ({rows}) AS {b} ON {b}.cell = {qident(mtab)}.{qident(route_col)}'
                    fw += f' JOIN {qident(R)} ON {qident(R)}.{qident("qid")} = {b}.qid'
                else:
                    rows = self._bridge_select(mtab, route_col, ("value", "world_key"), ("cell", "canon"))
                    fw += f' JOIN ({rows}) AS {b} ON {b}.cell = {qident(mtab)}.{qident(route_col)}'
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
