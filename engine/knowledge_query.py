"""KnowledgeQuery — the UNIFIED ENCODER wired into the LIVE /api/knowledge path, end to end.

This realizes the unified-encoder objective in production (not just /api/dimension analyze):
  * OPERATOR + OPERAND from the metric space  — inherited EncoderQuery.read_op_all (no MEASURE_NOUNS/table_noun);
    the delegate (aggregate / pure world-join) path is EntityQuery.serve, which calls THIS read_op_all via MRO.
  * BRIDGE TABLES persisted per user on Postgres (the thesis: "an interpretable model is a database"):
      "<t> connected to knowledgebase"   = the bridge of resolved FKs (cell -> world key + country), via bge +
                                       knowledgebase.words (exact entity resolution; same-space NOT required —
                                       the join is on a string key).
      "<t> unconnected to knowledgebase" = a unified-encoder vector(896) per free-text cell
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
    - read_op_all / read_op_model  resolve to EncoderQuery (the metric-space operator), NOT keywords.
    - serve / meaning_filter / _world_joins / route / _resolve  resolve to EntityQuery (the world machinery + bge).
    - schema / _encode / _layers  resolve to TableQuery, but run on the UNIFIED qwen (overlaid in __init__).
"""
from __future__ import annotations
import os
import re

import numpy as np

from engine.config import DATA_DIR, kb_model_route_enabled
from engine.entities import EntityQuery, PLACE_TYPES, WORLD_TABLE_TYPE
from engine.dataset_semantics import is_synthetic_currency_column
from engine.embeddings import Embedder, demonym_stems, pgvector_literal, normalize_surface
from engine.encoder_overlay import EncoderQuery, load_encoder, load_sql_selection
from engine.knowledge_bridges import KnowledgeBridgeMixin
from engine.knowledge_typing import KnowledgeTypingMixin
from engine.knowledge_tables import COUNT_CUE
from engine.bridge import STOP
from engine.closed_class import (
    EXCLUSION_CUES, action_words, closed_class_words, degree_words, measure_participles, measured_rows,
    noun_words,
)
from engine.currency_intent import (
    currency_conversion_target, currency_conversion_words, currency_rate_attribute,
)
from engine.calculations import calculation_clarify
from engine.numeric import parse_decimal
from engine.sql_schema import is_surrogate_key


def _cos(a, b):
    a = np.asarray(a, np.float32); b = np.asarray(b, np.float32)
    return float(a @ b / ((np.linalg.norm(a) * np.linalg.norm(b)) + 1e-9))


def _is_num(v):
    try:
        parse_decimal(v); return True
    except (ValueError, TypeError):
        return False


def _coverage_sql(response):
    """Coverage needs all emitted stages, including filters before the final reduction."""
    response = response or {}
    program = (response.get("deterministic") or {}).get("sql") or {}
    return program.get("source") or response.get("sql")


def verify_nonempty(res, question):
    """An aggregate over ZERO matching rows is not an answer.

    SQL returns a single all-NULL row for SUM/AVG/MIN/MAX over an empty relation, which rendered as
    [['']] and was returned with no clarify -- 'total budget in Africa' on a dataset with no African
    row looked exactly like a real answer. That blank is also what let a mis-routed world join fail
    SILENTLY rather than visibly (2026-09-08); an own-data filter matching nothing already clarifies,
    and this makes the aggregate path agree.

    It lives on KnowledgeQuery.serve because that is the ONE terminal every caller shares: evaluators
    call it directly, and compose keeps this delegate authoritative -- a composed re-expression only
    stands when it reproduces the delegate's answer, so a delegate clarify propagates.

    The aggregate test uses the TYPED evidence the planner already publishes (each output expression
    carries `aggregate_functions`), never the SQL text, and never `column_provenance` -- provenance is
    attached by provenance.decorate_response AFTER serve returns, so keying on it silently never fires.
    Narrow by construction: every output must be an aggregate and every cell empty, so a plain SELECT
    is untouched and COUNT -- which yields 0, never NULL -- stays a real answer.
    """
    if not isinstance(res, dict) or res.get("clarify") or res.get("error"):
        return res
    rows = (res.get("result") or {}).get("rows")
    if not rows or len(rows) != 1 or any(cell not in (None, "") for cell in rows[0]):
        return res
    outputs = [out for branch in ((res.get("computation") or {}).get("branches") or ())
               for out in (branch.get("outputs") or ())]
    if not outputs or not all(out.get("aggregate_functions") for out in outputs):
        return res                      # no typed proof this is an aggregate -> never second-guess it
    ops = " / ".join(sorted({str(fn) for out in outputs for fn in out["aggregate_functions"]}))
    return {"question": question, "as_of": res.get("as_of"), "clarify": True,
            "result": None, "error": None, "original_sql": res.get("sql"),
            "reason": f"no rows matched, so there is nothing to {ops}",
            "model": "engine - clarify (the query matched no rows)"}


def _calculation_coverage_words(calculations):
    """Words realized structurally by a satisfied typed calculation.

    Coverage normally checks that content words appear in SQL as identifiers, literals, or resolved
    entities. Direction words instead become AST operators: "after discount" becomes ``1 - rate``.
    Claim them only after the calculation verifier succeeds, and only for that operation, so a time
    predicate such as "after 2024" cannot disappear under a global stop-word rule.
    """
    claimed = set()
    for calculation in calculations or ():
        if calculation.get("status") != "satisfied":
            continue
        operation = calculation.get("operation")
        if operation in {"subtract_rate", "add_rate", "apply_rate"}:
            claimed.update({"based", "basis"})
        if operation == "subtract_rate":
            claimed.update({
                "after", "subtract", "subtracting", "subtracted", "deduct", "deducting",
                "deducted", "reduce", "reducing", "reduced", "apply", "applying", "applied", "net",
            })
        elif operation == "add_rate":
            claimed.update({
                "add", "adding", "added", "plus", "including", "inclusive", "with", "gross",
            })
        elif operation == "apply_rate":
            claimed.update({"apply", "applying", "calculate", "calculating", "compute", "computing"})
    return claimed


# Words for the spreadsheet itself rather than its data ('Count all non-empty Order ID rows below
# the header'): never a row filter, a world entity, or free text to search for.
_SPREADSHEET_WORDS = frozenset({
    "row", "rows", "column", "columns", "cell", "cells", "sheet", "sheets", "spreadsheet",
    "header", "headers", "blank", "empty", "non", "data",
})

# Words that ask for a computation: an aggregate or an extreme. The operator realizes them, so a question that
# uses one asks for a number or a ranking, and the semantic search (rows listed by similarity) never answers
# it. The operator readout models only COUNT/SUM/AVG; 'What is the highest amount paid?' went unread once the
# readout stopped counting articles, and 'paid' alone would have sent it to the search (2026-09-27). An adverb
# that asks for the total is one of them: 'the total population of these cities combined' was declined as
# having dropped 'combined' (2026-09-28).
_OPERATOR_WORDS = frozenset({
    "many", "much", "number", "average", "avg", "mean", "total", "sum", "count", "per", "each",
    "highest", "lowest", "largest", "smallest", "most", "least", "maximum", "minimum", "max", "min",
    "top", "bottom", "combined", "altogether", "overall",
})

# Question and aggregate words are realized by the OPERATOR, not by a filter: they are never a world entity
# (excluding them stops _best_world_entity from matching 'how'/'many' to a town and reporting a COUNT query as
# having "dropped" them, which hijacked 'how many ... in France' to clarify), and never free text to search
# for. The world ENTITY-TYPE nouns (cities, countries, ...) NAME the resolved type, never a filter value, so
# 'total amount for CITIES in France' must not report 'cities' dropped.
_QUERY_WORDS = _OPERATOR_WORDS | frozenset({
    "how", "list", "show", "give", "find", "get", "what", "which", "who", "whom", "where", "when",
    "are", "were", "city", "cities", "country", "countries", "state", "states", "town", "towns",
    "place", "places", "nation", "nations", "element", "elements", "atomic", "has",
    "named", "there", "among",
    # Positional words: 'Count all non-empty Order ID rows below the header in the Customers sheet' asks a
    # plain COUNT, and a weak embedding match to some town once reported the spreadsheet words as dropped
    # filters, hijacking the count to clarify (2026-09-14). Numeric comparators ('below 100') were never
    # covered by this guard (content words are alphabetic), so exempting 'below'/'above' loses no real
    # constraint coverage.
    "below", "above", "current",
    # Comparatives are comparators too. Once the counted noun was read by its part of speech, 'more' in
    # "how many deliveries weigh more than 3 kg" surfaced as a place the query dropped (2026-10-01).
    "more", "less", "fewer", "greater", "higher", "lower", "larger", "smaller", "bigger",
    # Spreadsheet scope prose. These words do not identify a row filter or world entity; treating them as
    # unresolved predicates turned an exact COUNT(DISTINCT "order ID") into a clarification about an unrelated
    # numeric column in production.
    "across",
    # Presentation/provenance language describes how to display the answer, not an additional row predicate.
    # Schema-named columns still win via the sheet's own vocabulary.
    "calculation", "calculations", "step", "steps", "reasoning", "analysis", "breakdown",
})


# Words that ask for a share of a total. Only a division realizes one.
_SHARE_WORDS = frozenset({"percentage", "percentages", "percent", "share", "proportion", "fraction"})

# The world types a question can ask for by name, each with the singular a column or a world join shows.
_WORLD_TYPE_WORDS = {
    "country": "country", "countries": "country", "city": "city", "cities": "city",
    "continent": "continent", "continents": "continent", "state": "state", "states": "state",
}


# Payment and listing states. 'paid' and 'listed' are prose in "the total amount paid" and "payments are
# listed", and a row filter where the data records such a state; _uncovered decides which.
_STATUS_WORDS = frozenset({"paid", "listed"})


def _word_forms(word):
    forms = {word, word.rstrip("s"), word + "s"}
    if word.endswith("y"):
        forms.add(word[:-1] + "ies")                        # city -> cities
    if word.endswith("ies"):
        forms.add(word[:-3] + "y")
    return forms


def _schema_vocabulary(sch):
    """The words the uploaded tables' own names and column names contribute, with plural forms.

    Split on ANY non-alphanumeric: Sheets columns are space-named ('order ID'), and an underscore-only
    split left 'order' unrecognized ('Count ... Order ID rows' clarified with 'order' reported dropped
    even though the column is literally named that, 2026-09-14). A 'city' column also covers 'cities'.
    """
    words = set()
    for column in sch:
        for name in (str(column["table"]).lower(), str(column["name"]).lower()):
            for part in {name} | set(re.split(r"[^a-z0-9]+", name)):
                if part:
                    words |= _word_forms(part)
    return words


def _residual_words(question, drop_surfaces):
    """Question words minus stopwords minus the surface tokens of any resolved world entity (those drive the
    structured filter)."""
    drop = set()
    for s in drop_surfaces:
        drop |= set(str(s).lower().split())
    words = "".join(ch.lower() if (ch.isalnum() or ch.isspace()) else " " for ch in question).split()
    return [w for w in words if w not in STOP and w not in drop]


def content_words(question, drop_surfaces=(), sch=(), closed=()):
    """The residual words that say something about the rows' content.

    Not a word that only names the tables, their columns or the spreadsheet: 'amount in France' is a column
    and a country, and the semantic path once ranked France's orders by similarity to the word 'amount',
    capped them at ten, and showed one opaque step instead of the lookup and filter (2026-09-27). Not a
    question or aggregate word (_QUERY_WORDS): 'highest' asks for a maximum. Not a closed-class word
    (``closed``, from engine.closed_class): 'everything in France' is a listing too.
    """
    other = _schema_vocabulary(sch) | _SPREADSHEET_WORDS | _QUERY_WORDS
    return [w for w in _residual_words(question, drop_surfaces) if w not in other and w not in closed]


def semantic_predicate(question, drop_surfaces=(), sch=(), closed=()):
    """The residual free-text predicate: 'who complained about bad delivery in France', France stripped ->
    drop who/about/in/complained (STOP) -> 'bad delivery'. Empty when the question asks for a computation
    (_OPERATOR_WORDS) or no residual word is content (``content_words``); otherwise the whole residual, so the
    embedding sees the question's phrase."""
    residual = _residual_words(question, drop_surfaces)
    if (EXCLUSION_CUES.search(question) or any(w in _OPERATOR_WORDS for w in residual)
            or not content_words(question, drop_surfaces, sch, closed)):
        return ""
    return " ".join(residual).strip()


class KnowledgeQuery(EncoderQuery, KnowledgeBridgeMixin, KnowledgeTypingMixin, EntityQuery):
    """Live /api/knowledge served by the unified encoder: bge for connected entity resolution, the unified encoder
    for the anchored readout + operator + the unconnected free-text bridge. Persists the two bridge tables per
    user and answers the hybrid structured+semantic query; delegates aggregates / plain world joins to
    EntityQuery."""

    FREETEXT_MIN_AVGLEN = 12       # a non-connected text column is "free text" (embed it) if avg cell length > this
    HYBRID_LIMIT = 10

    _SHARE = ("alloc", "nc", "dims", "sid", "thr", "model", "nL", "tok", "qwen", "hdim",
              "sql_proposer", "sql_arbiter")

    def __init__(self, deploy_dir=DATA_DIR):
        EntityQuery.__init__(self, deploy_dir)       # bge + Postgres + world metadata + spaCy
        load_encoder(self, deploy_dir)               # ONE MODEL: the trained encoder (operator+bridge+typing)
        load_sql_selection(self, deploy_dir)         # the own-data query proposer + arbiter
        self._schema_interpreter()                   # Schema.org head: a bundle it cannot load fails here
        # The planner composes a TableQuery (self.q11) for the single-table delegate path. Point it at the SAME
        # models (shared refs — one copy of each in memory) so EVERY path goes through the same weights.
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
        # The learned router types columns too, so it is the OTHER source that could propose a world
        # join for an engine-internal column. Both sources are filtered by the same predicate; a
        # synthesized measure-currency column carries an ISO code, never a world entity.
        routes.update({key: value for key, value in model_routes.items()
                       if not is_synthetic_currency_column(key[1])})
        typing.extend(item for item in model_typing
                      if not is_synthetic_currency_column(item.get("column")))
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
        # This runs once per uploaded ROW on the non-geo path, so both exact lookups go through the
        # request memo: repeated cell values and the constant type_qid->label lookup hit Postgres once.
        _r = self._kb_rows('SELECT label FROM knowledgebase."types" WHERE qid=%s', (type_qid,))
        wl = (str(_r[0][0]) if _r and _r[0][0] else label)            # the exact label used by the offline projection
        n = normalize_surface(value)
        rows = self._kb_rows('SELECT qid FROM knowledgebase."words" WHERE type=%s AND norm=%s AND qid IS NOT NULL LIMIT 1',
                             (wl, n))
        if rows:
            return rows[0][0]
        vec = pgvector_literal(Embedder.get().encode([value])[0])
        rows = self._kb_rows(
            'SELECT qid, 1-(embedding <=> %s::vector) FROM knowledgebase."words" WHERE type=%s AND qid IS NOT NULL '
            'ORDER BY embedding <=> %s::vector LIMIT 1', (vec, wl, vec))
        row = rows[0] if rows else None
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
                print(f"[knowledge_query] schema router unavailable: {type(exc).__name__}", flush=True)
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
                        print(f"[knowledge_query] column evidence unavailable: {type(exc).__name__}", flush=True)
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
                # English plural forms count as naming the type: hospital->hospitals (-s), university->universities
                # (-y/-ies), church->churches (-es); a bare "s?" missed every -ies plural and silently dropped the
                # university family to the clarify path.
                def _names_type(w):
                    stem = _re.escape(w[:-1]) + r"(?:y|ies)" if w.endswith("y") else _re.escape(w) + r"(?:s|es)?"
                    return _re.search(r"\b" + stem + r"\b", ql)
                if not any(_names_type(w) for w in wl.replace("_", " ").split() if len(w) > 3):
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
        rows = self._kb_rows(
            "SELECT type, COUNT(DISTINCT norm) FROM knowledgebase.\"words\" WHERE norm = ANY(%s) "
            "AND type NOT IN ('city','country','state','type') GROUP BY type ORDER BY 2 DESC LIMIT 1", (norms,))
        row = rows[0] if rows else None
        # >=50% of the DISTINCT cells must resolve to ONE non-geo type. The question must separately name that type,
        # so this deterministic fallback remains fail-closed when Schema.org classification abstains.
        if not row or row[1] < max(2, 0.5 * len(norms)):
            return None, None
        wl = row[0]
        q = self._kb_rows('SELECT qid FROM knowledgebase."types" WHERE label=%s LIMIT 1', (wl,))
        return (wl, q[0][0]) if q else (None, None)

    def _serve_world_type(self, norm, question, sch, plan, schema):
        """Aggregate an uploaded NON-GEO table joined to its faithful Wikidata world table, filtered by country.
        e.g. hospitals.csv(hospital, beds) + 'total beds for hospitals in United States' -> resolve each hospital to
        a pre-synchronized knowledgebase.\"hospital\" row, keep those whose .country = 'United States', SUM(beds).

        The per-cell entity resolution is the engine's resolver (Python); it persists the connected bridge,
        and the shared deterministic plan runs every relational step after it in both programs, returning the
        derivation trail (docs/SHEETS_AS_REASONING.md): lookup -> filtered -> total."""
        t = plan["table"]; label = plan["label"]; country = plan["country"]
        ci = t["columns"].index(plan["col"])
        op = (self.read_op_model([t], question)[0]) or "COUNT"
        measure = None
        if op in ("SUM", "AVG"):
            measure = next((c["name"] for c in sch if c["table"] == t["name"]
                            and c.get("affinity") in ("INTEGER", "REAL") and not is_surrogate_key(c["name"])), None)
            if not measure:
                op = "COUNT"

        resolved = []                                                     # (upload row, resolved world qid)
        for rw in t["rows"]:
            v = str(rw[ci]) if ci < len(rw) and rw[ci] not in (None, "") else None
            if not v:
                continue
            wq = self._resolve_world_qid(v, label, plan["qid"])
            if not wq:
                continue
            resolved.append((list(rw), wq))

        cur = self._rconn().cursor()
        cur.execute('SELECT label FROM knowledgebase."types" WHERE qid=%s', (plan["qid"],))
        _r = cur.fetchone(); wl = (str(_r[0]) if _r and _r[0] else label)[:63]   # table = the EXACT Wikidata label
        model = f'engine - non-geo world join (pre-synchronized knowledgebase."{wl}")'
        from engine.deterministic.context import current_analysis_context, current_execution_record
        context = current_analysis_context()
        if context is None:                                   # production enters one per request (engine/server.py)
            raise RuntimeError("world questions are served inside an analysis context")
        from engine.deterministic.world import lower_world_query, reference_schema
        from engine.numeric import wire_rows
        self._pg_schema = schema
        self.q11._pg_schema = schema
        self._persist_connected(t["name"], plan["col"], label,
                                [(str(row[ci]), qid) for row, qid in resolved])
        con = self._connect({table["name"]: table for table in norm}, sch, attach_world=True)
        try:
            shared_plan = lower_world_query(
                slug=context.slug, schema=sch, uploaded=[t["name"]], foreign_keys=[],
                joins=[{"left_table": t["name"], "left_col": plan["col"], "right_table": wl, "right_col": "qid"}],
                bridge_name=self._conn_bridge_name(t["name"]), route_table=t["name"], route_column=plan["col"],
                meaning_filter={"filter_table": wl, "attr": "country", "value": country},
                own_filters=[], world_rate=None, as_of=None, aggregate=(op, t["name"], measure),
                calculation=None, conversion=None, reference_columns=reference_schema(con, {wl: {"country"}}))
            release = self._bridge_world_version()
            con.conn.commit()
            columns, rows = self.q11._execute_deterministic({t["name"]: t}, shared_plan, release,
                                                            labels=self._qid_labels)
            record = current_execution_record()
            return {"question": question, "as_of": None, "sql": record["final_sql"],
                    "result": {"columns": columns, "rows": wire_rows(rows[:50])},
                    "views": record["views"], "deterministic": record, "model": model}
        finally:
            con.close()
            self._con = None

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

    # ---------------- clarify: detect a query that dropped part of the question + propose a rephrasing ----------------
    def _word_qid(self, w):
        """the world qid a single content word resolves to (exact normalized match in knowledgebase.\"words\" across the geo
        entity types), so _uncovered can tell a word is COVERED when its QID appears in the qid-keyed knowledgebase SQL."""
        try:
            r = self._kb_rows('SELECT qid FROM knowledgebase."words" WHERE norm=%s AND qid IS NOT NULL '
                              "AND type IN ('country','continent','city','state') LIMIT 1",
                              (normalize_surface(w),))
            return r[0][0] if r else None
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
        sch_words = _schema_vocabulary(sch)
        CUE = set(_QUERY_WORDS | _SPREADSHEET_WORDS)       # realized by the operator, never a filter (see _QUERY_WORDS)
        if _re.search(r'\breturn\s+(?:the|a|an|this|that)\b', question.lower()):
            CUE.add("return")
        if _re.search(r'\bcount\s*\(\s*distinct\b', sqll):
            # These words are realized by COUNT(DISTINCT ...), even though they do not occur
            # literally in the emitted SQL. A column actually named "value" is already covered by
            # sch_words, so this only closes the operator-language false-positive.
            CUE |= {"distinct", "unique", "different", "value", "values"}
        # A world type the question asks for ("which country", "by country") is realized only by a column of that
        # name or by a world join that brings one: "which country has the most deposits" ranked the banks, and
        # "total amount by country" summed every restaurant into one total (Chrome exploration, 2026-10-01). A
        # type noun that only names the rows ("total amount for cities in France") stays exempt.
        asked = [word for word in _WORLD_TYPE_WORDS
                 if _re.search(r"\b(?:which|what|by|per|each)\s+" + word + r"\b", question.lower())
                 and word not in sch_words and _WORLD_TYPE_WORDS[word] not in sch_words
                 and _WORLD_TYPE_WORDS[word] not in sqll]
        # Closed-class words carry grammar, not a constraint: 'everything in France' drops nothing. Negation and
        # exclusion cues are never closed-class here (engine.closed_class), so a dropped 'not' is still caught.
        closed = closed_class_words(question)
        literals = {word for c in sch for value in (c.get('values') or ())
                    if value is not None for word in _re.findall(r'[a-z]+', str(value).casefold())}
        # A finite verb or an adverb says what the rows did or how, never which rows. Each was declined as a
        # dropped town: "which item sold the most units" (sold), "how many documents are still pending"
        # (still), "how many deliveries weigh more than 3 kg" (weigh), "how many leads came from France"
        # (came) (Chrome exploration, 2026-10-01). A word the data holds as a value ("Sold") is still a row
        # filter, and so are the payment and listing states that the prose rule below decides.
        action = {w for w in action_words(question) if w not in literals and w not in _STATUS_WORDS}
        content = [w for w in _re.findall(r"[a-z]+", question.lower())
                   if w not in STOP and w not in CUE and len(w) > 1 and w not in closed
                   and w not in action and w not in sch_words and w.rstrip("s") not in sch_words]
        # A weak embedding match to a town must not reinterpret ordinary query
        # prose as geography. Only exempt bounded grammatical forms, and never
        # when a status column or an observed literal makes the word a real
        # business constraint. Invoices/orders are not evidence of payment.
        state_columns = any(
            set(_re.findall(r"[a-z]+", str(c['name']).lower())) &
            {'status', 'state', 'paid', 'listed', 'settled'} for c in sch
        )
        if has_agg and not state_columns and set(content) & _STATUS_WORDS:
            prose = set()
            if _re.search(r'\b(?:is|are|was|were)\s+listed\b', question, _re.I):
                prose.add('listed')
            payment_fact = any(
                set(_re.findall(r'[a-z]+', str(c['table']).lower())) & {'payment', 'payments'}
                for c in sch
            )
            if payment_fact and _re.search(r'\bamount\s+paid\b', question, _re.I):
                prose.add('paid')
            if literals & {'unpaid', 'unlisted', 'pending', 'settled', 'refunded'}:
                prose.clear()
            content = [word for word in content if word not in prose or word in literals]
        # Target words are covered only when SQL uses the exact direct-rate column for that
        # target. Unrelated arithmetic or a rate for another currency cannot bypass clarify.
        currency_target = currency_conversion_target(question)
        if (currency_target is not None and has_agg and "*" in sqll
                and currency_rate_attribute(currency_target) in sqll):
            realized = currency_conversion_words(currency_target)
            content = [word for word in content if word not in realized]
        if content and _re.search(r'\bcount\s*\(', sqll):
            # The noun a count cue governs is what COUNT counts: the head of the words after 'how many' /
            # 'number of', up to the first grammar word. 'how many leads from Europe' was declined because
            # 'leads' sat 0.85 from the city of Leeds; in 'how many German leads' 'German' is still checked.
            # The head is the run's last noun, so 'how many leads came from France' counts leads, not 'came'.
            # A ranking by the count reads the same way: "which country has the most leads" ranks by COUNT(*) and
            # was declined over 'leads' (Chrome exploration, 2026-10-01).
            cues = COUNT_CUE + (r"|most|fewest|least" if _re.search(r'\border\s+by\s+count\s*\(', sqll) else "")
            low = question.lower()
            nouns = noun_words(question)
            for cue in _re.finditer(r"\b(?:" + cues + r")\s+", low):
                run = []
                for token in _re.findall(r"[a-z]+|[.,;:!?]", low[cue.end():]):
                    if token in closed or not token.isalpha() or len(run) == 4:
                        break                                # a noun phrase ends at grammar or punctuation
                    run.append(token)
                if run:
                    heads = [token for token in run if token in nouns]
                    head = heads[-1] if heads else run[-1]
                    content = [word for word in content if word != head]
        if content and has_agg:
            # The same holds for the rows another aggregate is taken over: "the average score of the leads"
            # was declined over 'leads' (Chrome exploration, 2026-10-01). A word before them ('German leads')
            # is still checked.
            rows = measured_rows(question, frozenset(sch_words))
            # A participle on the measured column says how its values came about: "the total quantity
            # purchased" (2026-10-01). The payment and listing states stay with the prose rule.
            described = measure_participles(question, frozenset(sch_words)) - _STATUS_WORDS
            content = [word for word in content if word not in rows and word not in described]
        if content and _re.search(r'\border\s+by\b|[<>]', sqll):
            # A graded adjective is realized by the ordering or the comparison: "what was the most expensive
            # event" was declined over 'expensive' (Chrome exploration, 2026-10-01).
            graded = degree_words(question)
            content = [word for word in content if word not in graded]
        if content:
            # A place is ONE name however many words it has, and its demonym is that name plus '-n'/'-an'
            # ('European' Europe, 'North American' North America). When a span names a qid the query filters
            # on, its words are covered. Word by word, 'united' in 'the United Kingdom' surfaced another country,
            # 'north' surfaced a town called North, and 'European' surfaced Germany: each declined a correct
            # plan (2026-09-30).
            words = _re.findall(r"[a-z]+", question.lower())
            spans = {" ".join(words[i:i + n]): words[i:i + n]
                     for n in (3, 2, 1) for i in range(len(words) - n + 1)}
            forms = {phrase: {phrase, *demonym_stems(phrase)} for phrase in spans}
            qids = self._phrase_qids({form for group in forms.values() for form in group})
            covered = {word for phrase, group in forms.items()
                       if any(qid.lower() in sqll for form in group for qid in qids.get(form, ()))
                       for word in spans[phrase]}
            content = [word for word in content if word not in covered]
        # A share is realized only by a division: "what percentage of orders are from Paris" listed the Paris
        # customers (Chrome exploration, 2026-10-02).
        asked += [word for word in content if word in _SHARE_WORDS and not _re.search(r"/|\bdiv", sqll)]
        content = [word for word in content if word not in _SHARE_WORDS]
        if not content:
            return asked
        nonid = [c for c in sch if c.get("affinity") in ("INTEGER", "REAL") and not is_surrogate_key(c["name"])
                 and c.get("qvec") is not None]
        uv = self._encode(content) if nonid else None

        def measure(i):
            return bool(nonid) and max(_cos(uv[i], np.asarray(c["qvec"], np.float32)) for c in nonid) > 0.5

        dropped = []
        for i, w in enumerate(content):
            if _re.search(r"\b" + _re.escape(w) + r"\b", sqll):  # the word literally appears in the SQL (a filter value
                continue                                         # like continent='Asia') -> it WAS used; covered. This
            wq = self._word_qid(w)                               # qid-keyed SQL: a word is COVERED if its resolved QID
            if wq and wq.lower() in sqll:                        # appears (knowledgebase filters on qids, e.g. continent='Q46')
                continue
            if has_agg and w not in literals and w not in _STATUS_WORDS and measure(i):
                # A measure word the aggregate realizes, as the docstring says; the town check below used to
                # come first, and "which category brought in the most revenue" was declined (2026-10-01).
                continue
            if wq:
                # A place named exactly that the query never filtered on. The fuzzy check below knows only
                # countries and cities, so "which bank has the most deposits in Europe" ranked every bank in the
                # world (Chrome exploration, 2026-10-01).
                dropped.append(w)
                continue
            ent = self._best_world_entity([w])                   # also catches continents/currencies _best_world_entity
            #                                                      doesn't resolve (it only knows country/city).
            if ent:
                if ent[1].lower() not in sqll:               # resolved to an entity the query did NOT filter on
                    dropped.append(w)
                continue                                     # entity present in the SQL -> used
            if not has_agg and measure(i):                   # a measure word, but no aggregate applied -> dropped
                dropped.append(w)
        return asked + dropped

    def _phrase_qids(self, phrases):
        """The place qids each phrase names exactly (the resolver's exact-name lookup), in one lookup."""
        norms = {}
        for phrase in phrases:
            norm = normalize_surface(phrase)
            if norm:
                norms.setdefault(norm, []).append(phrase)
        try:
            by_type = self._names_by_type(set(norms))
        except Exception:                                        # noqa: BLE001 — same contract as _word_qid
            return {}
        found = {}
        for norm, types in by_type.items():
            qids = {qid for type_, group in types.items() if type_ in PLACE_TYPES for qid in group}
            for phrase in norms.get(norm, ()) if qids else ():
                found.setdefault(phrase, set()).update(qids)
        return found

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
        nonid = [c for c in sch if c.get("affinity") in ("INTEGER", "REAL") and not is_surrogate_key(c["name"])]
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
            print(f"[knowledge_query] country name lookup failed: {type(e).__name__}", flush=True)
            return None

    def serve(self, tables, question, as_of=None, schema=None, explicit_fks=(), dataset_semantics=()):
        """Hybrid structured+semantic retrieval when the question has a free-text predicate AND the data has a
        free-text column AND it is not an aggregate; otherwise delegate to EntityQuery (which uses the unified
        operator via read_op_all). Any hybrid error falls back to EntityQuery so the world path never hard-fails."""
        norm, fks = self.ingest(tables, explicit_fks=explicit_fks)
        sch, _, _ = self.schema(norm, fks)
        is_agg = self.read_op_all(question, sch) is not None
        if is_agg and schema:                                         # NON-GEO world join over synchronized facts
            ngp = None
            try:
                ngp = self._nongeo_plan(norm, question)
                if ngp:
                    return verify_nonempty(
                        self._serve_world_type(norm, question, sch, ngp, schema), question)
            except Exception as e:                                    # noqa: BLE001 — fall through to the geo/delegate path
                if ngp is not None:
                    return {"question": question, "error": f"{type(e).__name__}: {e}", "result": None}
                print(f"[knowledge_query] non-geo serving failed: {type(e).__name__}", flush=True)
        cr = None if is_agg else self._resolve(question, "country")   # (country QID, sim, surface) | None — resolved ONCE
        closed = frozenset() if is_agg else closed_class_words(question)
        # A quoted cell value ('GOLD customers', 'in Paris') is an exact filter, never words to search for.
        own = [] if is_agg else self._own_value_matches(question, norm)
        drop = ([cr[2]] if cr else []) + [value for _table, _column, value in own]
        pred = "" if is_agg else semantic_predicate(question, drop, sch, closed)
        plan = next((p for p in (self._table_plan(t) for t in norm) if p), None) if pred else None
        # The search reads one sheet; a filter on another sheet needs the structured path's joins.
        if plan and any(table != plan["table"]["name"] for table, _column, _value in own):
            plan = None
        if plan and pred and schema:
            try:
                # _resolve returns a country QID, but the connected bridge stores + filters/disambiguates on the
                # country NAME (canon_country). Map qid -> canonical name here; otherwise "in France" compares
                # 'Q142' against 'France' and the EXISTS matches nothing (silent empty result).
                cname = self._country_name_for_qid(cr[0]) if cr else None
                if cr and not cname:
                    cname = cr[2]                                      # last resort: the surface the user typed
                mention = tuple((w, tuple(sorted(_word_forms(w))))
                                for w in content_words(question, drop, sch, closed))
                return self._serve_hybrid(norm, fks, sch, question, pred, plan,
                                          cname, as_of, schema, mention,
                                          [(column, value) for _table, column, value in own])
            except Exception as e:                                   # noqa: BLE001 — never hard-fail the world path
                print(f"hybrid serve failed, delegating: {type(e).__name__}", flush=True)
        # Delegate the aggregate / plain-world-join path to EntityQuery EXPLICITLY (not super()): in this MRO
        # TableQuery precedes EntityQuery (EncoderQuery pulls TableQuery in early), so super().serve would hit the
        # 2-arg TableQuery.serve. EntityQuery.serve's own super() is relative to EntityQuery and correctly chains
        # RoutedQuery->PgQuery->KnowledgeTableQuery (skipping TableQuery). read_op_all inside that chain still resolves
        # to EncoderQuery's metric-space operator via MRO.
        res = verify_nonempty(
            EntityQuery.serve(self, tables, question, as_of=as_of, schema=schema,
                              explicit_fks=explicit_fks, dataset_semantics=dataset_semantics), question)
        if isinstance(res, dict) and res.get("decomposition_required"):
            return {
                "question": question,
                "as_of": as_of,
                "clarify": True,
                "decomposition_required": res["decomposition_required"],
                "model": "engine - typed AST decomposition requested",
            }
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
                dropped = self._uncovered(question, sch, _coverage_sql(res))
                if currency and currency.get("status") == "satisfied":
                    realized = currency_conversion_words(currency["target"])
                    dropped = [word for word in dropped if word not in realized]
                claimed = _calculation_coverage_words(calculations)
                dropped = [word for word in dropped if word not in claimed]
            except Exception as e:                           # noqa: BLE001 — the gate must never break the world path
                print(f"coverage check failed: {type(e).__name__}", flush=True); dropped = []
            if dropped:
                try:
                    c = self._clarify(question, norm, fks, sch)
                except Exception as e:                       # noqa: BLE001
                    print(f"clarify failed: {type(e).__name__}", flush=True); c = None
                # No rephrasing expresses a share: "what share of the total amount comes from Paris" was offered
                # "total unit price".
                rephrased = (bool(c) and c["proposed"].strip().lower() != (question or "").strip().lower()
                             and not set(dropped) & _SHARE_WORDS)
                # A world type or an exactly named place the query never realized is a dropped constraint whether
                # or not a rephrasing exists: "which country has the most deposits" served the top bank because
                # none was found (Chrome exploration, 2026-10-01).
                firm = [word for word in dropped
                        if word in _WORLD_TYPE_WORDS or word in _SHARE_WORDS or self._word_qid(word)]
                if rephrased or firm:
                    return {"question": question, "as_of": as_of, "clarify": True,
                            "original_sql": (res or {}).get("sql"),
                            "proposed": c["proposed"] if rephrased else "",
                            "bindings": c["bindings"] if rephrased else [], "dropped": dropped,
                            "calculations": calculations,
                            "computation": (res or {}).get("computation"),
                            "currency": currency,
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
