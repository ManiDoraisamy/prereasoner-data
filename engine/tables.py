"""Multi-table interpretable text->SQL over uploaded CSVs. N tables joined on DETERMINISTIC foreign keys
(engine.relations): FK discovery gives the join graph, the model only PICKS which tables/columns the
question refers to (learned intent + name/representation binding) and the SQL is assembled with qualified
`"t"."c"` identifiers, guarded SELECT-only, and executed on the request's SQLite database (engine/prepared.py).
Per-token readout is read PER LAYER through the SAME anchored model, so a JOIN's `"orders"."customer_id"`
fires is_field + the FK edge to `"customers"."customer_id"`.

TableQuery does NOT load its own encoder: in the serving closure it is always composed under the unified
encoder overlay (engine.knowledge_query.load_encoder shares device/alloc/nc/dims/sid/thr/model/nL/tok/qwen/hdim onto
it), so the ONE trained model drives every path.
"""
from __future__ import annotations
import csv as _csv
import io
import re
import sqlite3
import threading
from functools import wraps
from pathlib import Path

import numpy as np

from engine import request_timing
from engine.config import DATA_DIR, BASE_MODEL_ID as MODEL_ID  # noqa: F401 - public compatibility export
from engine.fk_edges import edges
from engine.numeric import parse_decimal, wire_decimal, observed_numeric_affinity, stored_as_numbers
from engine.relations import relate
from engine.request_validation import canonical_table_name
from engine.column_names import canonical_columns

MAX_ROWS, MAX_LEN = 12, 48
_ENCODE_CACHE_LOCK = threading.Lock()                    # TableQuery._encode: concurrent questions share the caches
FORBID =re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|truncate|vacuum|with)\b", re.I)


def _torch_no_grad(function):
    """Import torch only when a model-backed method is actually invoked."""

    @wraps(function)
    def wrapped(*args, **kwargs):
        import torch

        with torch.no_grad():
            return function(*args, **kwargs)

    return wrapped


def qident(s):
    return '"' + str(s).replace('"', '""') + '"'


def qlit(s):
    return "'" + str(s).replace("'", "''") + "'"


def qual(t, c):
    return f'{qident(t)}.{qident(c)}'


def normalize_table_name(name):
    """Return the canonical table identifier used throughout planner ingestion."""
    return re.sub(r"\W+", "_", str(name)).strip("_") or "t"


def normalize_tables(tables):
    """Return the canonical table/row representation used by planning and execution."""
    normalized = []
    for table in tables:
        original_columns = list(table["columns"])
        columns = canonical_columns(original_columns)
        rows = [row if isinstance(row, list) else [row.get(column) for column in original_columns]
                for row in table["rows"]]
        normalized.append({
            "name": normalize_table_name(table["name"]),
            "columns": columns,
            "rows": rows,
        })
    return normalized


def _fk_columns(fk, side):
    plural, singular = f"{side}_cols", f"{side}_col"
    return tuple(fk[plural]) if plural in fk else (fk[singular],)


def _fk_endpoint(fk, side):
    values = [qual(fk[f"{side}_table"], column) for column in _fk_columns(fk, side)]
    return values[0] if len(values) == 1 else values


def affinity(struct):
    if "is_num" in struct:
        return "REAL" if "num_frac" in struct else "INTEGER"
    if "is_bool" in struct:
        return "INTEGER"
    return "TEXT"


DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}([ T].+)?$")      # ISO dates (sort + substr-year safe as TEXT)
# The planner's reply when no runnable reading reads the whole question (engine/answer_presentation.UNREAD_REPLY).
NO_EXECUTABLE_CANDIDATE = "planner: no executable AST candidate"
READERS_MODEL = "engine - learned readers + typed SQL AST builder"


def name_words(name):
    return [w for w in re.split(r"[^a-z0-9]+", str(name).lower()) if w]


def wmatch(tok, w):
    """plural-insensitive word match: emails->email, countries->country, cities->city."""
    return (tok == w or tok == w + "s" or (tok.endswith("s") and tok[:-1] == w)
            or (tok.endswith("ies") and w.endswith("y") and tok[:-3] == w[:-1]))


# ---------- CSV parsing + dim labels (shared readout helpers) ----------
LABEL = {  # explicit dims for the analyze view; other families fall back to dim_label()
    "is_str": ("📄", "text", "#22a06b"), "is_num": ("🔢", "number", "#3b82f6"), "num_frac": ("➗", "decimal", "#6366f1"),
    "is_time": ("📅", "date/time", "#8b5cf6"), "is_bool": ("☑", "boolean", "#e0840a"), "is_enum": ("🏷️", "category", "#0ea5e9"),
    "is_key": ("🔑", "key", "#caa011"), "is_ref": ("🔗", "reference", "#0891b2"), "currency": ("💲", "currency", "#16a34a"),
    "nsmcat_person": ("🧑", "person·nsm", "#b45309"), "nsmcat_play": ("💬", "action·nsm", "#b45309"),
    "nsmcat_tag": ("🏷️", "kind·nsm", "#b45309"), "nsmcat_abacus": ("🧮", "quantity·nsm", "#b45309"),
    "nsmcat_chain": ("⛓️", "logic·nsm", "#b45309"), "nsmcat_scales": ("⚖️", "value·nsm", "#b45309"),
    "nsmcat_be": ("🟰", "being·nsm", "#b45309"), "nsmcat_clock": ("🕐", "time·nsm", "#b45309"), "nsmcat_pin": ("📍", "place·nsm", "#b45309"),
    "pos_NUM": ("🔢", "number", "#3b82f6"), "pos_NOUN": ("📛", "noun", "#0ea5e9"), "pos_PROPN": ("🔠", "proper noun", "#7c3aed"),
    "pos_ADJ": ("🎨", "adjective", "#db2777"), "pos_VERB": ("🏃", "verb", "#16a34a"), "pos_ADV": ("⏩", "adverb", "#65a30d"),
    "pos_SYM": ("➕", "symbol", "#6b7280"),
    "ner_DATE": ("📅", "date", "#8b5cf6"), "ner_CARDINAL": ("#️⃣", "count", "#2563eb"), "ner_ORG": ("🏢", "org", "#0891b2"),
    "ner_MONEY": ("💲", "money", "#16a34a"), "ner_GPE": ("📍", "place", "#dc2626"), "ner_PERSON": ("🧑", "person", "#d97706"),
    "ner_PERCENT": ("％", "percent", "#0d9488"), "ner_TIME": ("🕐", "time", "#9333ea"), "ner_FAC": ("🏛️", "facility", "#0369a1"),
}


def dim_label(dim):
    if dim in LABEL:
        return LABEL[dim]
    if dim.startswith("nsm_"):
        return ("✦", dim[4:].replace("_", " ") + "·nsm", "#7c3aed")   # NSM prime
    if dim.startswith("ace_"):
        return ("◆", dim[4:].replace("_", " "), "#c026d3")            # ACE common-noun class (entity type)
    return ("•", dim, "#888")


def _typed(v):
    v = v.strip() if isinstance(v, str) else v
    if v in ("", None) or not isinstance(v, str):
        return v
    try:
        numeric = parse_decimal(v)
        return int(numeric) if numeric == numeric.to_integral_value() and "." not in v and "e" not in v.lower() else numeric
    except ValueError:
        return v


def table_name(name, index=0):
    """Return the canonical SQL/planner name for an uploaded or saved table."""
    return canonical_table_name(name, index)


def _unquote(s):
    s = (s or "").strip() if isinstance(s, str) else s
    if isinstance(s, str) and len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def parse_rows(text):
    text = (text or "").strip()
    g = list(_csv.reader(io.StringIO(text), skipinitialspace=True))
    if len(g) < 1:
        return []
    width = max((index+1 for row in g for index, value in enumerate(row) if value.strip()), default=0)
    if not width:
        return []
    original = g[0][:width]
    header = canonical_columns([_unquote(h) for h in original] + [None] * (width-len(original)))
    rows = []
    for r in g[1:]:
        if not any((c or "").strip() for c in r):
            continue
        rows.append({key: _typed(_unquote(r[index])) if index < len(r) else None
                     for index, key in enumerate(header)})
    return rows


def csv_table(csv_text, name):
    rows = parse_rows(csv_text)
    cols = list(rows[0].keys()) if rows else []
    return {"name": name, "columns": cols, "rows": [[r.get(c) for c in cols] for r in rows]}


def table_from_rows(name, columns, rows):
    """Build a planner table from structured rows using the SAME cell typing as CSV uploads (parse_rows):
    _unquote then _typed. Without this, a saved-reference cell keeps any wrapping quotes while the same value
    uploaded as CSV is unquoted, so foreign-key discovery, which compares keys exactly as the join does, would
    drop the reference. Kept byte-identical so a master table is just another own-data table to the planner."""
    cols = canonical_columns([_unquote(None if column is None else str(column)) for column in columns or []])
    width = len(cols)
    typed_rows = []
    for row in rows or []:
        values = list(row or [])[:width]
        values += [None] * (width - len(values))
        typed_rows.append([_typed(_unquote(value)) for value in values])
    return {"name": name, "columns": cols, "rows": typed_rows}


class _PoolRuns:
    """The database ``TableQuery.select_query`` runs its pools on, the request's (engine/prepared.py) holding its
    tables, and whether each query it ran there finished and the shape of what it returned: a pool member is run
    once per selection."""

    def __init__(self, planner, tablemap, sch):
        self._tablemap, self._sch = tablemap, sch
        self._database = None
        self._own = None
        self.outcomes = {}
        # (rows, columns) of each query that ran: the result shape the re-scorer reads
        self.shapes = {}
        # Whether each query that ran matched no row: none came back, or an aggregate's one NULL total or zero count.
        self.matched_nothing = {}

    def database(self):
        if self._database is None:
            from engine.prepared import RequestDatabase, current_database
            self._database = current_database()
            if self._database is None:                    # outside a served request: a database of its own
                self._database = self._own = RequestDatabase()
        return self._database

    def connection(self):
        return self.database().load(self._tablemap, self._sch)

    def close(self):
        if self._own is not None:
            self._own.close()
        self._database = self._own = None


class TableQuery:
    def __init__(self, deploy_dir=DATA_DIR):
        # DEFERRED encoder: the serving closure never runs TableQuery standalone — engine.knowledge_query's
        # load_encoder OVERLAYS the trained encoder onto this instance right after construction (shared refs:
        # device/alloc/nc/dims/sid/thr/model/nL/tok/qwen/hdim — ONE model in memory). Nothing model-shaped is loaded
        # here, so startup does not pay for weights that would be immediately replaced.
        self.deploy_dir = Path(deploy_dir)
        self.alloc = None
        self.nc = None
        self.dims = []
        self.sid = {}
        self.thr = {}
        self.model = None
        self.nL = None
        self.tok = None
        self.qwen = None
        self.hdim = None
        self.device = "cpu"                                  # load_encoder sets the serving device

    # ---------- encoding ----------
    # The vector for a text depends only on the text and the loaded weights, so encoded texts are
    # cached on the instance that holds those weights (a new overlay instance starts empty — correct
    # invalidation by construction). Measured on the serve path before this cache existed: 49-70
    # texts per request, only 19 unique — column names encoded 5-8x within ONE turn and re-encoded
    # identically on every follow-up, at 3.5-5.4s per production request. Bounded LRU; ~hdim floats
    # per entry. Questions run in parallel, so its reads, writes and evictions hold _ENCODE_CACHE_LOCK
    # (one request's move_to_end raced another's eviction); the forward pass runs outside it.
    _ENCODE_CACHE_CAP = 8192

    @_torch_no_grad
    def _encode_batch(self, texts):
        """The raw forward pass, uncached, texts packed without padding (engine/encoder.py). Callers go through
        `_encode`."""
        from engine.encoder import pooled_vectors
        if not texts:
            return np.zeros((0, self.hdim), np.float32)
        return pooled_vectors(self.qwen, self.tok, texts, MAX_LEN, self.device).cpu().numpy().astype(np.float32)

    def _encode(self, texts):
        if self.qwen is None:
            raise RuntimeError("no encoder loaded — TableQuery must be overlaid with the trained encoder "
                               "(engine.knowledge_query.load_encoder / engine.encoder_overlay.EncoderQuery)")
        from collections import OrderedDict
        cache = self.__dict__.setdefault("_encode_cache", OrderedDict())
        request_timing.count("encode_texts", len(texts))
        out = np.zeros((len(texts), self.hdim), np.float32)
        miss_at = []                                       # positions whose text was not cached
        with _ENCODE_CACHE_LOCK:
            for i, t in enumerate(texts):
                vec = cache.get(t)
                if vec is not None:
                    cache.move_to_end(t)
                    out[i] = vec
                else:
                    miss_at.append(i)
        if miss_at:
            uniq = list(dict.fromkeys(texts[i] for i in miss_at))   # a text may repeat WITHIN one call
            request_timing.count("encode_miss", len(uniq))
            with request_timing.span("encode"):
                fresh = self._encode_batch(uniq)
            by_text = {t: fresh[j] for j, t in enumerate(uniq)}
            for i in miss_at:
                out[i] = by_text[texts[i]]
            with _ENCODE_CACHE_LOCK:
                for t, vec in by_text.items():
                    cache[t] = vec.copy()                  # own copy — callers may mutate `out` rows
                while len(cache) > self._ENCODE_CACHE_CAP:
                    cache.popitem(last=False)
        return out

    @_torch_no_grad
    def _layers(self, units, x):
        import torch

        E = edges(units); S = len(units)
        outs = self.model.forward_layers(torch.from_numpy(x)[None], torch.from_numpy(E)[None],
                                         torch.zeros(1, S, dtype=torch.bool))
        return [o[0].detach().numpy() for o in outs]

    def _fires(self, vec, fam, thr=0.5):
        return [d["name"].split("_", 1)[1] for d in self.dims
                if d["family"] == fam and vec[d["dim_id"]] >= self.thr.get(d["name"], thr)]

    # ---------- ingest + schema ----------
    def ingest(self, tables, explicit_fks=()):
        """Normalize tables without changing source row multiplicity and discover trusted joins."""
        norm = normalize_tables(tables)
        g = relate(norm, explicit_fks=explicit_fks)
        return g["tables"], g["fks"]

    def _schema_name_units(self, tables, fks):
        """One name-only schema unit per (table, column), GLOBAL col index, ref=(to_table,to_col) on FK cols."""
        ref_of = {}
        for fk in fks:
            from_cols = _fk_columns(fk, "from")
            to_cols = _fk_columns(fk, "to")
            ref_of.update({
                (fk["from_table"], from_col): (fk["to_table"], to_col)
                for from_col, to_col in zip(from_cols, to_cols)
            })
        units, colidx = [], {}
        for t in tables:
            for c in t["columns"]:
                colidx[(t["name"], c)] = len(colidx)
                units.append({"text": str(c), "group": "schema", "kind": "name", "table": t["name"],
                              "col": colidx[(t["name"], c)], "colname": c, "row": -1,
                              "ref": ref_of.get((t["name"], c))})
        return units, colidx

    def schema(self, tables, fks):
        """The planner's schema of the tables: one record per column (its encoder reading, affinity and
        values), the global column index, and the tables by name. Built once per request
        (`relations.memoized`): each record and mapping is the caller's own copy."""
        from engine.relations import content_key, memoized
        sch, colidx, tablemap = memoized(
            "planner_schema", lambda: (id(self.model), content_key(tables), repr(fks)),
            lambda: self._schema(tables, fks))
        return [dict(column) for column in sch], dict(colidx), dict(tablemap)

    def _schema(self, tables, fks):
        units, colidx = self._schema_name_units(tables, fks)
        x = self._encode([u["text"] for u in units])
        final = self._layers(units, x)[-1]
        pos = {(u["table"], u["colname"]): i for i, u in enumerate(units)}
        sch = []
        for t in tables:
            rowdicts = [dict(zip(t["columns"], r)) for r in t["rows"]]
            for c in t["columns"]:
                i = pos[(t["name"], c)]; vec = final[i]
                struct = {d["name"] for d in self.dims if d["family"] == "struct" and vec[d["dim_id"]] >= 0.5}
                ace = sorted(((dim_label(d["name"])[1], vec[d["dim_id"]]) for d in self.dims if d["family"] == "ace"),
                             key=lambda z: -z[1])
                vals = [rd.get(c) for rd in rowdicts]
                ne = [v for v in vals if v is not None and str(v).strip() != ""]
                if stored_as_numbers(ne):
                    aff = observed_numeric_affinity(ne)
                elif ne and not all(isinstance(v, bool) for v in ne):
                    # A learned numeric label cannot turn notes or formula errors
                    # into missing numbers. Keep the cells and prevent arithmetic
                    # on this column; other columns and row counts remain usable.
                    aff = "TEXT"
                else:
                    aff = affinity(struct)
                sch.append({"table": t["name"], "name": str(c), "idx": colidx[(t["name"], c)], "struct": struct,
                            "affinity": aff, "ace": [lb for lb, s in ace[:3] if s >= 0.4],
                            "is_date": bool(ne) and all(DATE_RE.match(str(v).strip()) for v in ne),
                            "qvec": x[i], "values": vals})
        return sch, colidx, {t["name"]: t for t in tables}

    def _link(self, qvec, tok, cand_sch, kind="any"):
        if kind == "num":
            cand = [c for c in cand_sch if c["affinity"] in ("INTEGER", "REAL")]
        elif kind == "cat":
            cand = [c for c in cand_sch if c["affinity"] == "TEXT"]
        else:
            cand = list(cand_sch)
        cand = cand or cand_sch
        for c in cand:
            n = c["name"].lower()
            if tok and (tok == n or tok.rstrip("s") == n.rstrip("s") or tok in n.split()):
                return c
        sims = [(c, float(qvec @ c["qvec"] / ((np.linalg.norm(qvec) * np.linalg.norm(c["qvec"])) + 1e-6))) for c in cand]
        sims.sort(key=lambda z: -z[1])
        return sims[0][0] if sims and sims[0][1] > 0.3 else None

    # ---------- planning ----------
    def search_pool(self, question, norm, fks, sch, graph=None):
        """The readers' candidates under the pool contract: ``select_query``'s first stage
        (engine.readers.search.ReadingSearch.pool), with the registered calculations' expansions of them
        (engine.calculations.search). Its top candidate is the readers' most probable reading. The readers read
        one table of each group of layout copies (``one_of_each_layout``)."""
        from engine.calculations.search import CalculationQueryExpander
        from engine.readers.search import reading_search
        from engine.sql_expansion import plain_marks
        from engine.sql_schema import SchemaGraph
        question = plain_marks(question)
        if graph is None:
            graph = SchemaGraph.from_planner(sch, fks)
        with request_timing.span("readers"):
            pool = reading_search().pool(question, one_of_each_layout(graph, question))
        return pool + CalculationQueryExpander(graph).expand(question, pool)

    def select_query(self, question, norm, fks, sch, tablemap, searched=None, graph=None):
        """Choose the query to serve for an own-data question; returns a ``PoolSelection``.

        1. The readers read the question against the schema and build their most probable readings as
           typed ASTs (``search_pool``, engine/readers/search.py).
        2. Every candidate that passes the guard is run on an in-memory SQLite copy of the
           request's tables, under a step budget that grows with the cells of the tables it reads
           (engine/sql_rank.execution_op_limit). A query that fails cannot be chosen, and
           neither can one that tests a text column against a literal the column never holds
           while another column does, or one that joins two columns the foreign keys keep apart
           (engine/sql_grounding.py).
        3. The readers' re-scorer orders the eligible candidates by the question, each one's read-as line
           and the shape of its result. A date the question names (engine/sql_dates) keeps the choice to
           the candidates that realize it, when one does; a candidate whose SUM or AVG reads only rows its
           joins repeat (engine/sql_grounding.py) is chosen only when every one does; and a registered
           calculation intent (engine/calculations) takes the best-ranked candidate that satisfies it.
        4. The readers' learned refusal serves the choice only when it is confident of it
           (``ReadingSearch.confidence``, recorded as ``confidence``) and the question is no longer than the
           questions its threshold was matched on (engine/decomposition.py:ONE_QUERY_WORDS); otherwise nothing is
           selected, the refused choice is recorded as ``declined``, and the reply says how it read the question
           (or, for a long question in an analysis, asks for the decomposition proposal).

        This is the one own-data selection: serving, decomposition leaves, the Spider evaluator and
        the offline regression gate all call it. The decomposition probe reads only its first stage
        (``search_pool``); a caller that already ran that stage passes its pool as ``searched``.
        """
        from engine.sql_expansion import plain_marks
        from engine.sql_schema import SchemaGraph

        # The question as its readers parse it: typographic quotes as the ASCII ones, comparison symbols as words
        # (sql_expansion.plain_marks).
        question = plain_marks(question)
        # One schema graph serves the request: building one indexes every cell value, which
        # took seconds a build on a few thousand rows, four times a question (2026-10-04).
        if graph is None:
            graph = SchemaGraph.from_planner(sch, fks)
        if searched is None:
            searched = self.search_pool(question, norm, fks, sch, graph=graph)
        runs = _PoolRuns(self, tablemap, sch)
        try:
            return self._choose(question, norm, sch, tablemap, graph, searched, runs)
        finally:
            runs.close()

    def _choose(self, question, norm, sch, tablemap, graph, pool, runs):
        """Run, ground, re-score and choose among one pool (steps 2 to 4 of ``select_query``)."""
        from dataclasses import replace

        from engine.calculations import select_calculation_candidate, detect_calculations
        from engine.decomposition import one_query_length
        from engine.readers.search import reading_search
        from engine.sql_dates import realizes_dates, served_date_phrases
        from engine.sql_expansion import tokens as question_tokens
        from engine.sql_grounding import absent_value, double_counted_members, grounded_members, shared_headers
        from engine.currency_intent import currency_conversion_target
        from engine.sql_rank import PoolSelection, mixed_currencies, outread_by_rows, select_ranked_candidate

        pool = tuple(pool)
        executable = self._executable(pool, tablemap, runs)
        with request_timing.span("pool_grounding"):
            grounded = grounded_members(pool, tablemap, graph)
            double_counted = double_counted_members(pool, tablemap)
        readers = reading_search()
        shapes = [runs.shapes.get(member.sql) if ran else None for member, ran in zip(pool, executable)]
        with request_timing.span("rescore"):
            scores = readers.rescore(question, pool, shapes)
        # A reading of one of two columns that share a header, the question not saying which, is not eligible.
        shared = tuple(shared_headers(member.query, tablemap, question) for member in pool)
        ranking = tuple(sorted((index for index, (ran, sound) in enumerate(zip(executable, grounded))
                                if ran and sound and not shared[index]),
                               key=lambda i: (-(scores[i] if scores[i] is not None else -1e9), i)))
        calculation_satisfied = [False] * len(pool)
        if detect_calculations(question):
            for index in ranking:
                _, assessments, _ = select_calculation_candidate(
                    question, norm, graph, [pool[index]])
                calculation_satisfied[index] = bool(assessments) and all(
                    row.get("status") == "satisfied" for row in assessments)
        phrases = served_date_phrases(question, question_tokens(question), graph)
        date_satisfied = [realizes_dates(member.query, phrases) for member in pool]
        request_timing.count("pool", len(pool))
        undecided = self._undecided(pool, ranking, runs)
        outread = outread_by_rows(pool, ranking, [runs.matched_nothing.get(member.sql, False) for member in pool])
        selection = PoolSelection(pool, tuple(executable), tuple(grounded), ranking, None,
                                  tuple(calculation_satisfied), tuple(date_satisfied), double_counted, undecided,
                                  shared, outread=outread,
                                  currencies={} if currency_conversion_target(question) else mixed_currencies(graph))
        selected = select_ranked_candidate(ranking, calculation_satisfied, date_satisfied, double_counted, undecided,
                                           outread)
        confidence = readers.confidence(pool, scores, shapes, selected) if selected is not None else None
        # The choice's confidence speaks only for questions as long as those its threshold was matched on: a longer
        # one is declined however confident (engine/decomposition.py:ONE_QUERY_WORDS). "Find the top 2 product
        # categories by revenue and the top 2 customers by total spend, then list each pair where ..." was read as
        # one ranking at 0.19 by the 2026-10-23 bundle, above its threshold of 0.1434.
        if not readers.confident(confidence) or not one_query_length(question):
            return replace(selection, confidence=confidence, declined=selected)
        absent = absent_value(pool[selected].query, graph) if selected is not None else None
        if absent is not None:
            return replace(selection, confidence=confidence, declined=selected, absent_value=absent)
        return replace(selection, selected=selected, confidence=confidence)

    @staticmethod
    def _undecided(pool, ranking, runs):
        """Per pool member: whether it is an eligible ranking whose order ranks none of the rows it keeps above the
        first row it leaves out (engine/sql_rank.ranking_cut_probe, ranks_nothing), on the copy of the request's
        tables in ``runs``."""
        from engine.sql_ast import render_query
        from engine.sql_rank import ranking_cut_probe, ranks_nothing

        undecided = [False] * len(pool)
        for index in ranking:
            probe = ranking_cut_probe(pool[index].query)
            if probe is None:
                continue
            query, limit, terms = probe
            try:
                rows = runs.connection().execute(render_query(query)).fetchmany(limit + 1)
            except sqlite3.Error:
                continue
            undecided[index] = ranks_nothing(rows, limit, terms)
        return tuple(undecided)

    def _serve_ast(self, question, norm, fks, sch, tablemap, graph=None):
        """Select the own-data query (``select_query``) and execute it through this executor."""
        from engine.deterministic.context import current_analysis_context
        analysis_context = current_analysis_context()
        searched = None
        if analysis_context is not None:
            from engine.decomposition import compound_candidate

            searched = self.search_pool(question, norm, fks, sch, graph=graph)
            compound = compound_candidate(searched)
            if compound is not None:
                # A named compound request needs a branch proposal before it has an
                # executable dual-emitter plan. Do not run a single query that answers one
                # fragment of the question and then throw its rows away: the search's own
                # reading asks for the decomposition (tests.test_complex_datasets).
                return compound, None, None, tuple(searched), None
        selection = self.select_query(question, norm, fks, sch, tablemap, searched=searched, graph=graph)
        candidates = selection.pool
        candidate = selection.candidate
        if candidate is None:
            if not candidates:
                return None, None, "planner: no valid AST candidate", candidates, selection
            return None, None, NO_EXECUTABLE_CANDIDATE, candidates, selection
        deterministic_plan = None
        if analysis_context is not None:
            from engine.decomposition import single_branch

            # One dual-emitter branch serves a named request, so a set operation gives way to the
            # best-ranked single SELECT; compound questions are decomposed (engine/decomposition.py).
            selection = selection.constrained(single_branch)
            candidate = selection.candidate
            if candidate is None:
                return None, None, "planner: no single-query AST candidate", candidates, selection
        ok, why = self.guard(candidate.sql)
        if not ok:
            return candidate, None, "guard: " + why, candidates, selection
        if analysis_context is not None:
            from engine.deterministic.lower import (
                UnsupportedDeterministicPlan,
                lower_select_query,
            )

            try:
                deterministic_plan = lower_select_query(
                    analysis_context.slug, candidate.query, sch, fks,
                )
            except UnsupportedDeterministicPlan:
                deterministic_plan = None
        try:
            cols, rows = self.execute(
                tablemap, sch, candidate.sql, query=candidate.query,
                deterministic_plan=deterministic_plan,
            )
            result = {
                "columns": cols,
                "rows": [["" if value is None else value for value in row] for row in rows],
            }
            return candidate, result, None, candidates, selection
        except Exception as exc:  # execution errors are returned in the serving envelope
            return candidate, None, f"{type(exc).__name__}: {exc}", candidates, selection

    # ---------- guard + execute ----------
    def guard(self, sql):
        s = sql.strip().rstrip(";")
        if ";" in s:
            return False, "multiple statements"
        if not re.match(r"(?is)\s*select\b", s):
            return False, "not a SELECT"
        if FORBID.search(s):
            return False, "forbidden keyword"
        return True, "ok"

    def _executable(self, pool, tablemap, runs):
        """Which pooled queries pass the guard and run to completion within their SQLite VM step
        budget (``engine.sql_rank.execution_op_limit``, which grows with the cells of the tables a
        query reads) on ``runs``, the request's one in-memory copy of its tables. The step budget is
        deterministic and machine-independent; it stops a pathological join, not a normal query.
        Success only makes a query eligible; it is not evidence that the query answers the question.
        A query ``runs`` already ran keeps its outcome."""
        if not pool:
            return ()
        from engine.request_deadline import expired, remaining
        from engine.sql_rank import execution_op_limit
        steps = budget = interval = 0
        def progress():
            nonlocal steps
            steps += interval
            return int(steps >= budget or expired())
        outcomes = []
        with request_timing.span("pool_execute"):
            try:
                for candidate in pool:
                    remaining()
                    if candidate.sql in runs.outcomes:
                        outcomes.append(runs.outcomes[candidate.sql])
                        continue
                    ran = False
                    if self.guard(candidate.sql)[0]:
                        con = runs.connection()
                        steps, budget = 0, execution_op_limit(candidate.query, tablemap)
                        interval = max(1, min(10000, budget))
                        con.set_progress_handler(progress, interval)
                        try:
                            # Eligibility needs completion, but not a second materialized copy
                            # of tens of thousands of result rows for each candidate.
                            cursor = con.execute(candidate.sql)
                            rows, first = 0, None
                            while chunk := cursor.fetchmany(1024):
                                first = chunk[0] if first is None else first
                                rows += len(chunk)
                                remaining()
                            runs.shapes[candidate.sql] = (rows, len(cursor.description or ()))
                            runs.matched_nothing[candidate.sql] = rows == 0 or (
                                rows == 1 and all(value in (None, 0) for value in first))
                            ran = True
                        except sqlite3.Error:
                            ran = False
                    runs.outcomes[candidate.sql] = ran
                    outcomes.append(ran)
            finally:
                if runs.outcomes:
                    runs.database().restore_deadline()    # the request's own statements end at its deadline
        return tuple(outcomes)

    def execute(self, tablemap, sch, sql, query=None, deterministic_plan=None):
        """Run one query on the request's database (engine/prepared.py), outside a served request on a database of
        its own: a deterministic plan through both programs (``_execute_deterministic``), else the query in the
        exact decimal dialect."""
        from engine.prepared import scratch_database
        from engine.sql_ast import SetQuery, SQLType, render_query, value_type

        with scratch_database() as database:
            con = database.load(tablemap, sch)
            if deterministic_plan is not None:
                return self._execute_deterministic(tablemap, deterministic_plan)
            execution_sql = render_query(query, dialect="sqlite_decimal") if query is not None else sql
            cur = con.execute(execution_sql)
            rows = cur.fetchall()
            columns = [d[0] for d in cur.description]
        if query is not None:
            output = query.left if isinstance(query, SetQuery) else query
            types = [value_type(item.expression) for item in output.select]
            normalized = []
            for row in rows:
                values = []
                for index, value in enumerate(row):
                    if index < len(types) and types[index] in {SQLType.INTEGER, SQLType.REAL} and isinstance(value, str):
                        try:
                            value = wire_decimal(parse_decimal(value, enforce_input_bounds=False))
                        except ValueError:
                            pass
                    values.append(value)
                normalized.append(tuple(values))
            rows = normalized
        return columns, rows

    def _execute_deterministic(self, tablemap, plan, knowledgebase_release=None, labels=None):
        """Run a served plan through both emitters on the request's database. ``labels`` resolves knowledgebase
        entities to their labels for display (docs/SHEETS_AS_REASONING.md rule 5); a plan that reads the
        knowledgebase cannot be shown without it, so every world trail passes it here, the one executor they
        share."""
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        from engine.analysis import analysis_input_hash
        from engine.config import (APP_ENV, deterministic_execution_mode, deterministic_persist_generated,
                                   deterministic_python_row_limit)
        from engine.deterministic.context import current_analysis_context, set_execution_record
        from engine.deterministic.service import DeterministicAnalysis
        from engine.prepared import current_database

        context = current_analysis_context()
        if context is None:
            raise RuntimeError("deterministic execution requires a named-analysis context")
        if labels is None and any(table.schema == "knowledgebase" for table in plan.tables):
            raise RuntimeError("a plan that reads the knowledgebase needs its entity labels")
        database = current_database()
        if database is None:
            raise RuntimeError("deterministic execution runs on the request's database")
        shared = _SharedConnection(database.connection)
        engine = create_engine("sqlite+pysqlite://", creator=lambda: shared, poolclass=StaticPool)
        result = DeterministicAnalysis(
            plan,
            conversation_schema="main",
            dataset_version=context.dataset_version or analysis_input_hash(list(tablemap.values())),
            knowledgebase_release=knowledgebase_release,
            labels=labels,
        ).run(
            engine,
            mode=context.execution_mode or deterministic_execution_mode(),
            estimated_rows=sum(
                len(tablemap[name].get("rows") or ())
                for name in {
                    table_name
                    for view in plan.views
                    if hasattr(view, "tables")
                    for table_name in view.tables
                }
                if name in tablemap
            ),
            python_row_limit=deterministic_python_row_limit(),
            app_env=APP_ENV,
            persist_generated=deterministic_persist_generated(),
            conversation_id=context.conversation_id,
            revision=context.revision,
        )
        set_execution_record(result.record())
        shown = result.output_rows()
        if shown:
            columns = list(shown[0])
        else:
            columns = list(plan.view_columns()[str(plan.output)])
        rows = [tuple(row.get(column) for column in columns) for row in shown]
        return columns, rows

    # ---------- analytics (the /dimension per-cell view) ----------
    def _salient_evo(self, layers, ui):
        """per-layer evolution dict for unit `ui`, over the DATA families. salient = dims fired at the final
        layer (ace>=0.4, else >=0.5) U the argmax -> small payload."""
        fams = {"struct", "nsm_cat", "nsm_prime", "ace"}
        ddims = [d for d in self.dims if d["family"] in fams]
        fin = layers[-1][ui]
        fired = [d["name"] for d in ddims if fin[d["dim_id"]] >= (0.4 if d["family"] == "ace" else 0.5)]
        amax = max(ddims, key=lambda d: fin[d["dim_id"]])["name"]
        salient = sorted(set(fired) | {amax})
        return [{nm: round(float(min(1.0, max(0.0, layers[L][ui][self.sid[nm]]))), 3) for nm in salient}
                for L in range(self.nL)]

    def analyze(self, table, max_rows=24, table_unit=False):
        """Per-column + per-cell named-dim readout PER LAYER (the analytics contract), served by the SAME
        model as the SQL path so the /dimension view shows exactly what the planner reads. Single table.
        table_unit=True adds ONE table-NAME unit (e.g. "Cities in the World"), wired to its column names by E_COL."""
        cols = list(table["columns"])
        rows = [dict(zip(cols, r)) for r in table["rows"][:max_rows]]
        units = [{"text": str(c), "group": "schema", "kind": "name", "table": table["name"], "col": ci,
                  "colname": c, "row": -1, "ref": None} for ci, c in enumerate(cols)]
        cellpos = {}
        for ri, rd in enumerate(rows):
            for ci, c in enumerate(cols):
                v = rd.get(c)
                if v is None or str(v).strip() == "":
                    continue
                cellpos[(ci, ri)] = len(units)
                units.append({"text": str(v), "group": "schema", "kind": "value", "table": table["name"],
                              "col": ci, "colname": c, "row": ri, "ref": None})
        tpos = None
        if table_unit:                                    # appended LAST so column/value indices above are unchanged
            tpos = len(units)
            units.append({"text": str(table["name"]), "group": "schema", "kind": "table", "table": table["name"],
                          "col": -1, "colname": None, "row": -1, "ref": None})
        x = self._encode([u["text"] for u in units])
        layers = self._layers(units, x)
        columns = [{"name": str(c), "evolution": self._salient_evo(layers, ci)} for ci, c in enumerate(cols)]
        out_rows = []
        for ri, rd in enumerate(rows):
            cells = []
            for ci, c in enumerate(cols):
                v = rd.get(c); val = "" if v is None else str(v)
                ui = cellpos.get((ci, ri))
                cells.append({"col": str(c), "value": val,
                              "evolution": self._salient_evo(layers, ui) if ui is not None else []})
            out_rows.append({"cells": cells})
        out = {"columns": columns, "rows": out_rows, "cols": [str(c) for c in cols], "n_layers": self.nL,
               "model": "engine - Qwen encoder + relational model; anchored named-dim readout (same model as multi-table SQL)"}
        if tpos is not None:
            out["table_name"] = str(table["name"]); out["table_evolution"] = self._salient_evo(layers, tpos)
        return out

    def serve(self, tables, question, explicit_fks=()):
        """tables: [{name, columns, rows}]. Full multi-table pipeline for the web UI."""
        from engine.sql_schema import SchemaGraph
        norm, fks = self.ingest(tables, explicit_fks=explicit_fks)
        sch, colidx, tablemap = self.schema(norm, fks)
        graph = SchemaGraph.from_planner(sch, fks)
        try:
            candidate, result, err, candidates, selection = self._serve_ast(
                question, norm, fks, sch, tablemap, graph=graph
            )
        except Exception as exc:
            candidate, result, candidates, selection = None, None, (), None
            err = f"{type(exc).__name__}: {exc}"
        clarification = declined = None
        if candidate is None and err == NO_EXECUTABLE_CANDIDATE and selection is not None and selection.ranking:
            from engine.decomposition import declined_compound
            from engine.deterministic.context import current_analysis_context

            if current_analysis_context() is not None:
                declined = declined_compound(selection, question)
            if declined is None:
                refused = selection.pool[selection.declined if selection.declined is not None else selection.ranking[0]]
                clarification = (absent_value_clarification(refused, selection.absent_value)
                                 if selection.absent_value else unsure_clarification(refused))
            err = None
        elif candidate is None and selection is not None and not selection.ranking and any(selection.shared_headers):
            # Every runnable reading read one of two columns that share a header: ask which.
            clarification = shared_header_clarification(next(h for h in selection.shared_headers if h)[0])
            err = None
        model = READERS_MODEL
        sql = candidate.sql if candidate is not None else None
        computation = None
        if candidate is not None:
            from engine.calculations import describe_computation
            computation = describe_computation(candidate.query)
        response = {
            "question": question,
            "sql": sql,
            "valid": candidate is not None and err is None,
            "error": err,
            "result": result,
            "tables": [{
                "name": table["name"], "columns": table["columns"],
                "n_rows": len(table["rows"]),
            } for table in norm],
            "fks": [{
                "from": _fk_endpoint(fk, "from"),
                "to": _fk_endpoint(fk, "to"),
                "conf": fk["conf"],
            } for fk in fks],
            "join": None,
            "schema": [{
                "table": column["table"], "name": column["name"],
                "affinity": column["affinity"], "ace": column["ace"],
            } for column in sch],
            "tokens": [],
            "ast": repr(candidate.query) if candidate is not None else None,
            "candidate_count": len(candidates),
            "evidence": list(candidate.evidence) if candidate is not None else [],
            "features": dict(candidate.features) if candidate is not None else {},
            "computation": computation.record() if computation is not None else None,
            "selection": selection.record() if selection is not None else None,
            "read_as": read_as_of(candidate),
            "model": model,
        }
        if clarification is not None:
            response.update(clarification)
        if candidate is not None:
            # The readers' learned refusal accepted this reading: it is the own-data planner's certificate
            # (engine/knowledge_query.py keeps its residual wording gate for the routes without one).
            from engine.query_contract import Coverage
            response['coverage'] = Coverage(True).record()
            from engine.sql_ast import share_output
            if share_output(candidate.query):
                response["unit"] = "percent"         # a share of a whole: 0.3 is stated as 30%
            copies = _unread_copies(question, candidate.query, graph)
            if copies:
                response["layout_copies"] = copies   # the answer says which of them it read
            from engine.calculations import assess_calculations
            from engine.calculations.registry import attach_calculation_evidence
            assessments = assess_calculations(
                question,
                norm,
                graph,
                computation,
            )
            attach_calculation_evidence(response, assessments)
        from engine.deterministic.context import current_execution_record
        deterministic = current_execution_record()
        if deterministic is not None:
            response["sql"] = deterministic["final_sql"]
            response["views"] = deterministic["views"]
            response["deterministic"] = {
                key: value for key, value in deterministic.items()
                if key not in {"views", "final_sql"}
            }
        elif declined is not None:
            response["decomposition_required"] = declined
        elif candidate is not None:
            from engine.deterministic.context import current_analysis_context
            from engine.decomposition import selected_decomposition_required

            required = selected_decomposition_required(candidate)
            if current_analysis_context() is not None and required:
                response["decomposition_required"] = required
        return response


class _SharedConnection:
    """The request's database connection as SQLAlchemy's pool hands it out: closing it leaves it open for the rest
    of the request, which closes it (engine/prepared.py)."""

    __slots__ = ("_connection",)

    def __init__(self, connection):
        object.__setattr__(self, "_connection", connection)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_connection"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_connection"), name, value)

    def close(self):
        pass


def one_of_each_layout(graph, question):
    """``graph`` with one table of each group of layout copies (``SchemaGraph.layout_copies``): the copy the question
    names, else the first. Copies hold one layout's rows for different products or months, such as the NT, SI and FF
    subscription exports; the readers read the question against one of them, and the answer names the others
    (``_unread_copies``). Read against a workbook of three exports and their three reports, every reading of "What is
    the total Amount broken down by Plan and Currency?" joined copies that no foreign key connects (tests.test_scale,
    2026-10-23)."""
    words = set(name_words(question))
    dropped = set()
    for group in graph.layout_copies:
        named = [table for table in group if set(name_words(table)) <= words]
        dropped |= set(group) - {(named or group)[0]}
    return graph.without(frozenset(dropped)) if dropped else graph


def _unread_copies(question, query, graph):
    """{"read": [table], "others": [...]}: the other tables that could answer a one-table reading when the
    question names no table, else None: copies of its layout (`SchemaGraph.layout_copies`), and near copies
    (one layout holds the other's, as an export with one more column does) that hold every column it reads.
    An answer from one of three subscription exports (NT, SI and FF) says which, so it is not taken for the
    workbook's total (2026-10-04). A table that only shares a key or a name (orders' customer_id) is not one."""
    from engine.relations import layout
    from engine.sql_ast import column_refs
    referenced = query.referenced_tables()
    if len(referenced) != 1:
        return None
    (table,) = referenced
    if set(name_words(table)) <= set(name_words(question)):
        return None
    read = layout(column.name for column in column_refs(query))
    own = layout(column.ref.name for column in graph.by_table[table])
    copies = next((group for group in graph.layout_copies if table in group), ())

    def near_copy(other):
        theirs = layout(column.ref.name for column in graph.by_table[other])
        return len(own & theirs) >= 3 and (own <= theirs or theirs <= own) and read <= theirs

    others = [other for other in graph.tables if other != table and (other in copies or near_copy(other))]
    if not others:
        return None
    return {"read": [table], "others": others}


def read_as_of(candidate):
    """The readers' plain reading of ``candidate`` (engine/readers/read_as.py), or None for a query the readers
    did not build."""
    evidence = getattr(candidate, "evidence", None) or ()
    first = evidence[0] if evidence else None
    return first if isinstance(first, str) and first else None


def shared_header_clarification(header):
    """The reply when every runnable reading reads one of two columns that share ``header`` and the question does not
    say which (engine/sql_grounding.shared_headers)."""
    return {"clarify": True, "dropped": [], "read_as": None,
            "reason": f"More than one column is headed “{header}”. Which one do you mean? Name it with its column "
                      f"letter, such as “{header} in column B”."}


def unsure_clarification(candidate):
    """The reply when the readers' refusal is not confident of any runnable reading: how the best-ranked one read
    the question, so the person can see what was understood and ask again in the sheet's own terms."""
    read_as = read_as_of(candidate)
    if read_as:
        reason = (f"I read this as “{read_as}”, but I'm not confident that is what you asked. "
                  "Ask again with the column names as your sheet spells them.")
    else:
        reason = ("I'm not confident I read this question right. "
                  "Ask again with the column names as your sheet spells them.")
    return {"clarify": True, "reason": reason, "dropped": [], "read_as": read_as}


def absent_value_clarification(candidate, absent):
    """The reply when the chosen reading tests a column of few values against a value it does not hold: what it read,
    and what the column does hold, so the person can say which they meant in one word."""
    from engine.readers.text import words
    column, value, values = absent
    listed = ", ".join(values[:-1]) + " and " + values[-1] if len(values) > 1 else values[0]
    return {"clarify": True, "dropped": [], "read_as": read_as_of(candidate),
            "reason": f"Your {words(column)} column holds {listed}, and I can't tell which of those “{value}” means. "
                      "Which did you mean?"}


def sch_col_of(agg, sch):
    if not agg or agg[1] is None:
        return None
    return next((c for c in sch if c["table"] == agg[1] and c["name"] == agg[2]), None)
