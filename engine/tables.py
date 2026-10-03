"""Multi-table interpretable text->SQL over uploaded CSVs. N tables joined on DETERMINISTIC foreign keys
(engine.relations): FK discovery gives the join graph, the model only PICKS which tables/columns the
question refers to (learned intent + name/representation binding) and the SQL is assembled with qualified
`"t"."c"` identifiers, guarded SELECT-only, and executed on an in-memory SQLite with every table created.
Per-token readout is read PER LAYER through the SAME anchored model, so a JOIN's `"orders"."customer_id"`
fires is_field + the FK edge to `"customers"."customer_id"`.

TableQuery does NOT load its own encoder: in the serving closure it is always composed under the unified
encoder overlay (engine.knowledge_query.load_encoder shares alloc/nc/dims/sid/thr/model/nL/tok/qwen/hdim onto
it), so the ONE trained model drives every path.
"""
from __future__ import annotations
import csv as _csv
import io
import re
import sqlite3
from functools import wraps
from pathlib import Path

import numpy as np

from engine import request_timing
from engine.config import DATA_DIR, BASE_MODEL_ID as MODEL_ID  # noqa: F401 - public compatibility export
from engine.fk_edges import edges
from engine.numeric import parse_decimal, register_sqlite_decimal, sqlite_numeric, wire_decimal
from engine.relations import relate
from engine.request_validation import canonical_table_name
from engine.column_names import canonical_columns

MAX_ROWS, MAX_LEN = 12, 48
FORBID = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|truncate|vacuum|with)\b", re.I)


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


def name_words(name):
    return [w for w in re.split(r"[^a-z0-9]+", str(name).lower()) if w]


def wmatch(tok, w):
    """plural-insensitive word match: emails->email, countries->country, cities->city."""
    return (tok == w or tok == w + "s" or (tok.endswith("s") and tok[:-1] == w)
            or (tok.endswith("ies") and w.endswith("y") and tok[:-3] == w[:-1]))


def _num_str(v):
    try:
        parse_decimal(v); return True
    except (TypeError, ValueError):
        return False


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
    uploaded as CSV is unquoted, so master.relevant_tables' case-sensitive value-inclusion guard would drop the
    reference. Kept byte-identical so a master table is just another own-data table to the planner."""
    cols = canonical_columns([_unquote(None if column is None else str(column)) for column in columns or []])
    width = len(cols)
    typed_rows = []
    for row in rows or []:
        values = list(row or [])[:width]
        values += [None] * (width - len(values))
        typed_rows.append([_typed(_unquote(value)) for value in values])
    return {"name": name, "columns": cols, "rows": typed_rows}


class TableQuery:
    def __init__(self, deploy_dir=DATA_DIR):
        # DEFERRED encoder: the serving closure never runs TableQuery standalone — engine.knowledge_query's
        # load_encoder OVERLAYS the trained encoder onto this instance right after construction (shared refs:
        # alloc/nc/dims/sid/thr/model/nL/tok/qwen/hdim — ONE model in memory). Nothing model-shaped is loaded
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
        # The labelled Gemini fallback of select_query (engine/question_rewrite.py); EncoderQuery sets it.
        self.question_rewriter = None

    # ---------- encoding ----------
    # The vector for a text depends only on the text and the loaded weights, so encoded texts are
    # cached on the instance that holds those weights (a new overlay instance starts empty — correct
    # invalidation by construction). Measured on the serve path before this cache existed: 49-70
    # texts per request, only 19 unique — column names encoded 5-8x within ONE turn and re-encoded
    # identically on every follow-up, at 3.5-5.4s per production request. Bounded LRU; ~hdim floats
    # per entry. Single-request serving today (WORLD_LOCK) — a concurrency refactor must revisit
    # this cache alongside _kb_rows.
    _ENCODE_CACHE_CAP = 8192

    @_torch_no_grad
    def _encode_batch(self, texts):
        """The raw forward pass, uncached. Callers go through `_encode`."""
        out = np.zeros((len(texts), self.hdim), np.float32)
        for i in range(0, len(texts), 64):
            chunk = texts[i:i + 64]
            enc = self.tok(chunk, return_tensors="pt", padding=True, truncation=True, max_length=MAX_LEN)
            h = self.qwen(**enc).last_hidden_state
            m = enc["attention_mask"].unsqueeze(-1).float()
            out[i:i + len(chunk)] = ((h * m).sum(1) / m.sum(1).clamp(min=1.0)).float().numpy()
        return out

    def _encode(self, texts):
        if self.qwen is None:
            raise RuntimeError("no encoder loaded — TableQuery must be overlaid with the trained encoder "
                               "(engine.knowledge_query.load_encoder / engine.encoder_overlay.EncoderQuery)")
        from collections import OrderedDict
        cache = self.__dict__.setdefault("_encode_cache", OrderedDict())
        request_timing.count("encode_texts", len(texts))
        out = np.zeros((len(texts), self.hdim), np.float32)
        miss_at = []                                       # positions whose text was not cached
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
            for t, vec in by_text.items():
                cache[t] = vec.copy()                      # own copy — callers may mutate `out` rows
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
                if ne and all(_num_str(v) for v in ne):
                    aff = "REAL" if any("." in str(v) for v in ne) else "INTEGER"
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
    def ast_semantic_signals(self, question, sch):
        """Encode role-specific question phrases in the same metric space as schema columns."""
        from engine.calculations.registry import (
            CALCULATION_INTENT_PROTOTYPES,
            calculation_operand_queries,
        )
        from engine.sql_rank import SemanticSignals, semantic_role_phrases
        phrases = semantic_role_phrases(question)
        columns = [c for c in sch if c.get("qvec") is not None]
        if not columns or getattr(self, "qwen", None) is None:
            return SemanticSignals.empty()
        tables = sorted({c["table"] for c in sch})
        roles = list(phrases)
        calculation_names = list(CALCULATION_INTENT_PROTOTYPES)
        calculation_phrases = [CALCULATION_INTENT_PROTOTYPES[name] for name in calculation_names]
        operand_queries = calculation_operand_queries(question)
        vectors = self._encode(
            [phrases[role] for role in roles]
            + tables
            + calculation_phrases
            + [phrase for _, phrase in operand_queries]
        )
        role_vectors = {role: vectors[i] for i, role in enumerate(roles)}
        table_vectors = {table: vectors[len(roles) + i] for i, table in enumerate(tables)}

        def cosine(a, b):
            av = np.asarray(a, np.float32); bv = np.asarray(b, np.float32)
            return float(av @ bv / ((np.linalg.norm(av) * np.linalg.norm(bv)) + 1e-9))

        column_roles = {
            role: {(c["table"], c["name"]): cosine(vector, c["qvec"]) for c in columns}
            for role, vector in role_vectors.items()
        }
        global_vector = role_vectors["global"]
        table_global = {table: cosine(global_vector, vector) for table, vector in table_vectors.items()}
        calculation_start = len(roles) + len(tables)
        calculation_intents = {
            name: cosine(global_vector, vectors[calculation_start + index])
            for index, name in enumerate(calculation_names)
        }
        operand_start = calculation_start + len(calculation_names)
        calculation_operands = {
            key: {
                (column["table"], column["name"]): cosine(
                    vectors[operand_start + index], column["qvec"]
                )
                for column in columns
            }
            for index, (key, _) in enumerate(operand_queries)
        }
        return SemanticSignals(
            column_roles,
            table_global,
            calculation_intents=calculation_intents,
            calculation_operands=calculation_operands,
        )

    def search_ast(self, question, sch, tables, fks, beam_size=64, max_candidates=25,
                   use_semantic_signals=True, rank_candidates=True, expand_recursive=True,
                   expand_constraints=True, expand_extrema=True):
        """Return ranked, typed SQL AST candidates from the deterministic search.

        Bounded typed-AST search with hand-written, inspectable ranking. This is the first half of
        own-data planning; ``select_query`` runs the candidates and serves the best one that runs.
        ``tables`` stays in the signature to make the boundary explicit; the rich ``sch`` already
        carries its values and inferred types.
        """
        from engine.sql_search import SQLSearcher, SchemaGraph
        graph = SchemaGraph.from_planner(sch, fks)
        searcher = SQLSearcher(graph, beam_size=beam_size, max_candidates=max_candidates)
        baseline_signals = (
            self.ast_semantic_signals(question, sch)
            if use_semantic_signals else None
        )
        return searcher.search(
            question, semantic_signals=baseline_signals,
            rank_candidates=rank_candidates,
            expand_recursive=expand_recursive,
            expand_constraints=expand_constraints,
            expand_extrema=expand_extrema,
        )

    def search_pool(self, question, norm, fks, sch):
        """The deterministic search's candidates under the pool contract: ``select_query``'s
        first stage. Its top candidate is the search's structural reading of the question."""
        from engine.sql_rank import SEARCH_CANDIDATES
        return self.search_ast(question, sch, norm, fks, max_candidates=SEARCH_CANDIDATES)

    def select_query(self, question, norm, fks, sch, tablemap, searched=None, allow_fallback=True):
        """Choose the query to serve for an own-data question; returns a ``PoolSelection``.

        1. The deterministic search ranks up to ``SEARCH_CANDIDATES`` typed ASTs (``search_pool``).
        2. Every candidate that passes the guard is run on an in-memory SQLite copy of the
           request's tables under a fixed step budget. A query that fails cannot be chosen, and
           neither can one that tests a text column against a literal the column never holds
           while another column does, or one that joins two columns the foreign keys keep apart
           (engine/sql_grounding.py).
        3. The best-ranked eligible candidate is served. A date the question names
           (engine/sql_dates) keeps the choice to the candidates that realize it, when one does; a
           registered calculation intent (engine/calculations) takes the best-ranked candidate that
           satisfies it, when one exists; and a money noun that names its table ("what's the sales in
           London") takes the best-ranked candidate that aggregates a money column
           (engine/sql_expansion.money_total_columns).
        4. When no candidate is eligible or the selected plan leaves request words unread, and the
           operator enabled Gemini, it may reword the question once (engine/question_rewrite.py). Gemini
           sees schema names and types, never cell values or conversation history. The deterministic
           typed search runs on that wording and replaces the baseline only when its reading is more
           specific.

        This is the one own-data selection: serving, decomposition leaves, the Spider evaluator and
        the offline regression gate all call it. The decomposition probe reads only its first stage
        (``search_pool``); a caller that already ran that stage passes its pool as ``searched``.
        Decomposition leaves pass ``allow_fallback=False`` (engine/decomposition.py:_leaf_readings).
        """
        from dataclasses import replace
        from engine.sql_schema import SchemaGraph

        if searched is None:
            searched = self.search_pool(question, norm, fks, sch)
        graph = SchemaGraph.from_planner(sch, fks)
        selection = self._choose(question, norm, sch, tablemap, graph, searched)
        fallback = self.question_rewriter
        selected_index = selection.selected
        calculation_satisfied = (selected_index is not None and
                                  selection.calculation_satisfied[selected_index])
        unread = _query_has_unread_terms(
            question, selection.candidate, graph,
            calculation_satisfied=calculation_satisfied,
        )
        needs_rewrite = selection.selected is None or unread
        if not needs_rewrite:
            return selection
        if not allow_fallback or fallback is None:
            return replace(selection, selected=None) if unread else selection
        if not fallback.available:
            if selection.selected is None or not unread:
                return selection
            from engine.sql_rank import FallbackRecord

            return replace(selection, selected=None, fallback=FallbackRecord(
                "none", fallback.model, note="Gemini unavailable"))
        return self._fall_back(question, norm, fks, sch, tablemap, graph, selection, fallback,
                               reject_baseline=selection.selected is not None and unread)

    def _choose(self, question, norm, sch, tablemap, graph, pool):
        """Run, ground and choose among one pool (steps 2 and 3 of ``select_query``)."""
        from dataclasses import replace

        from engine.calculations import select_calculation_candidate, detect_calculations
        from engine.sql_dates import realizes_dates, served_date_phrases
        from engine.sql_expansion import aggregates_money_column, money_total_columns
        from engine.sql_expansion import tokens as question_tokens
        from engine.sql_grounding import grounded_members
        from engine.sql_rank import EXECUTION_OP_LIMIT, PoolSelection, select_ranked_candidate

        pool = tuple(pool)
        executable = self._executable(pool, tablemap, sch, EXECUTION_OP_LIMIT)
        with request_timing.span("pool_grounding"):
            grounded = grounded_members(pool, tablemap, graph)
        ranking = tuple(index for index, (ran, sound) in enumerate(zip(executable, grounded))
                        if ran and sound)
        calculation_satisfied = [False] * len(pool)
        if detect_calculations(question):
            for index in ranking:
                _, assessments, _ = select_calculation_candidate(
                    question, norm, graph, [pool[index]])
                calculation_satisfied[index] = bool(assessments) and all(
                    row.get("status") == "satisfied" for row in assessments)
        money_total = [False] * len(pool)
        money = money_total_columns(question, sch)
        if money is not None:
            table, columns = money
            names = [column["name"] for column in columns]
            for index in ranking:
                money_total[index] = aggregates_money_column(pool[index].query, table, names)
        phrases = served_date_phrases(question, question_tokens(question), graph)
        date_satisfied = [realizes_dates(member.query, phrases) for member in pool]
        from engine.query_contract import constraint_violations, coverage
        ranking = tuple(index for index in ranking
                        if not constraint_violations(question, pool[index].query, graph))
        request_timing.count("pool", len(pool))
        selection = PoolSelection(pool, tuple(executable), tuple(grounded), ranking, None,
                                  tuple(calculation_satisfied), tuple(money_total),
                                  tuple(date_satisfied))
        selected = select_ranked_candidate(ranking, calculation_satisfied, money_total, date_satisfied)
        if selected is not None and not coverage(question, pool[selected], graph,
                calculation_satisfied=calculation_satisfied[selected]).complete:
            complete = tuple(index for index in ranking if coverage(question, pool[index], graph,
                calculation_satisfied=calculation_satisfied[index]).complete)
            if complete:
                selected = select_ranked_candidate(complete, calculation_satisfied, money_total, date_satisfied)
        return replace(selection, selected=selected)

    def _fall_back(self, question, norm, fks, sch, tablemap, graph, selection, fallback,
                   reject_baseline=False):
        """Step 4 of ``select_query``: one isolated rewrite, followed by the same typed search."""
        from dataclasses import replace

        from engine.sql_rank import FallbackRecord

        with request_timing.span("fallback"):
            rewritten, note = fallback.rewrite(question, graph)
            if rewritten is None:
                baseline = replace(selection, selected=None) if reject_baseline else selection
                return replace(baseline, fallback=FallbackRecord("none", fallback.model, note=note))
            reread = self._choose(rewritten, norm, sch, tablemap, graph,
                                  self.search_pool(rewritten, norm, fks, sch))
            if reread.selected is not None:
                rewritten_is_complete = not _query_has_unread_terms(
                    rewritten, reread.candidate, graph,
                    calculation_satisfied=(
                        reread.selected is not None and
                        reread.calculation_satisfied[reread.selected]
                    ),
                )
                if not rewritten_is_complete:
                    baseline = replace(selection, selected=None) if reject_baseline else selection
                    return replace(baseline, fallback=FallbackRecord(
                        "none", fallback.model, question=rewritten,
                        note="the rewritten question still has unread terms"))
                if (selection.selected is not None and not reject_baseline and
                        not _rewrite_improves_reading(selection, reread)):
                    baseline = replace(selection, selected=None) if reject_baseline else selection
                    return replace(baseline, fallback=FallbackRecord(
                        "none", fallback.model, question=rewritten,
                        note="the deterministic search's original reading was stronger"))
                return replace(reread, fallback=FallbackRecord(
                    "rewrite", fallback.model, question=rewritten))
            baseline = replace(selection, selected=None) if reject_baseline else selection
            return replace(baseline, fallback=FallbackRecord(
                "none", fallback.model, question=rewritten,
                note="the search found no runnable query for the rewording"))

    def _serve_ast(self, question, norm, fks, sch, tablemap):
        """Select the own-data query (``select_query``) and execute it through this executor."""
        from engine.deterministic.context import current_analysis_context
        analysis_context = current_analysis_context()
        searched = None
        if analysis_context is not None:
            from engine.decomposition import compound_candidate

            searched = self.search_pool(question, norm, fks, sch)
            compound = compound_candidate(searched)
            if compound is not None:
                # A named compound request needs a branch proposal before it has an
                # executable dual-emitter plan. Do not run a single query that answers one
                # fragment of the question and then throw its rows away: the search's own
                # reading asks for the decomposition (tests.test_complex_datasets).
                return compound, None, None, tuple(searched), None
        selection = self.select_query(question, norm, fks, sch, tablemap, searched=searched)
        candidates = selection.pool
        if not candidates:
            return None, None, "planner: no valid AST candidate", candidates, selection
        candidate = selection.candidate
        if candidate is None:
            from engine.query_contract import constraint_violations
            from engine.sql_schema import SchemaGraph
            graph = SchemaGraph.from_planner(sch, fks)
            for member in candidates:
                for violation in constraint_violations(question, member.query, graph):
                    if violation.startswith('Which repeated field'):
                        return None, None, violation, candidates, selection
            return None, None, "planner: no executable AST candidate", candidates, selection
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
                    postgres_row_identity=getattr(self, "postgres_row_identity", False),
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

    def _sqlite_tables(self, tablemap, sch):
        """An in-memory SQLite database holding the request's normalized tables."""
        con = sqlite3.connect(":memory:")
        register_sqlite_decimal(con)
        by_t = {}
        for c in sch:
            by_t.setdefault(c["table"], []).append(c)
        for tname, cols in by_t.items():
            declarations = []
            for column in cols:
                storage = "TEXT COLLATE decimal" if column["affinity"] == "REAL" else column["affinity"]
                declarations.append(f'{qident(column["name"])} {storage}')
            con.execute(f"CREATE TABLE {qident(tname)} (" + ", ".join(declarations) + ")")

            def coerce(v, aff):
                if v is None:
                    return None
                if aff in ("INTEGER", "REAL"):
                    return sqlite_numeric(v, aff)
                return str(v)
            t = tablemap[tname]
            ins = f"INSERT INTO {qident(tname)} VALUES ({','.join('?' * len(cols))})"
            for r in t["rows"]:
                rd = dict(zip(t["columns"], r))
                con.execute(ins, [coerce(rd.get(c["name"]), c["affinity"]) for c in cols])
        return con

    def _executable(self, pool, tablemap, sch, op_limit):
        """Which pooled queries pass the guard and run to completion within ``op_limit`` SQLite
        VM steps on one in-memory copy of the request's tables. The step budget is deterministic
        and machine-independent; it stops a pathological join, not a normal query. Success only
        makes a query eligible; it is not evidence that the query answers the question."""
        if not pool:
            return ()
        con = self._sqlite_tables(tablemap, sch)
        from engine.request_deadline import expired, remaining
        steps = 0
        interval = max(1, min(10000, int(op_limit)))
        def progress():
            nonlocal steps
            steps += interval
            return int(steps >= op_limit or expired())
        con.set_progress_handler(progress, interval)
        outcomes = []
        with request_timing.span("pool_execute"):
            for candidate in pool:
                remaining()
                steps = 0
                if not self.guard(candidate.sql)[0]:
                    outcomes.append(False)
                    continue
                try:
                    # Eligibility needs completion, but not a second materialized copy
                    # of tens of thousands of result rows for each candidate.
                    cursor = con.execute(candidate.sql)
                    while cursor.fetchmany(1024):
                        remaining()
                    outcomes.append(True)
                except sqlite3.Error:
                    outcomes.append(False)
        con.close()
        return tuple(outcomes)

    def execute(self, tablemap, sch, sql, query=None, deterministic_plan=None):
        """Run one query on an in-memory SQLite copy of the tables (the local executor; the live
        service executes on PostgreSQL through engine.pg)."""
        from engine.sql_ast import SetQuery, SQLType, expression_type, render_query

        con = self._sqlite_tables(tablemap, sch)
        execution_sql = render_query(query, dialect="sqlite_decimal") if query is not None else sql
        cur = con.execute(execution_sql)
        rows = cur.fetchall()
        if query is not None:
            output = query.left if isinstance(query, SetQuery) else query
            types = [expression_type(item.expression) for item in output.select]
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
        return [d[0] for d in cur.description], rows

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
        norm, fks = self.ingest(tables, explicit_fks=explicit_fks)
        sch, colidx, tablemap = self.schema(norm, fks)
        try:
            candidate, result, err, candidates, selection = self._serve_ast(
                question, norm, fks, sch, tablemap
            )
        except Exception as exc:
            candidate, result, candidates, selection = None, None, (), None
            err = f"{type(exc).__name__}: {exc}"
        fallback = selection.fallback if selection is not None else None
        served_by = selection.served_by if selection is not None else "search"
        # The question the served query answers: Gemini's rewording when the search answered that.
        answered = fallback.question if served_by == "gemini-rewrite" else question
        model = "engine - typed SQL AST planner (deterministic search)"
        if served_by == "gemini-rewrite":
            model = (f"engine - typed SQL AST planner; the search read {fallback.model}'s rewording "
                     "of the question")
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
            "fallback": fallback.record() if fallback is not None else None,
            "model": model,
        }
        if candidate is not None:
            from engine.query_contract import coverage
            from engine.sql_schema import SchemaGraph
            checked_question = fallback.question if fallback is not None and fallback.kind == 'rewrite' else question
            response['coverage'] = coverage(
                checked_question, candidate, SchemaGraph.from_planner(sch, fks),
                calculation_satisfied=bool(selection is not None and selection.selected is not None
                                           and selection.calculation_satisfied[selection.selected]),
            ).record()
            from engine.sql_ast import share_output
            if share_output(candidate.query):
                response["unit"] = "percent"         # a share of a whole: 0.3 is stated as 30%
            from engine.calculations import assess_calculations
            from engine.calculations.registry import attach_calculation_evidence
            from engine.sql_schema import SchemaGraph
            assessments = assess_calculations(
                answered,
                norm,
                SchemaGraph.from_planner(sch, fks),
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
        elif candidate is not None:
            from engine.deterministic.context import current_analysis_context
            from engine.decomposition import selected_decomposition_required

            required = selected_decomposition_required(candidate)
            if current_analysis_context() is not None and required:
                response["decomposition_required"] = required
        return response


def _query_has_unread_terms(question, candidate, graph, *, calculation_satisfied=False):
    from engine.query_contract import coverage
    return not coverage(question, candidate, graph,
                        calculation_satisfied=calculation_satisfied).complete


def _rewrite_improves_reading(original, rewritten):
    """A rewording can replace the baseline only when it makes the typed reading more specific."""
    from engine.sql_ast import SelectQuery, SetQuery, Star, SubquerySource

    def has_star(query):
        if isinstance(query, SetQuery):
            return has_star(query.left) or has_star(query.right)
        if not isinstance(query, SelectQuery):
            return False
        if isinstance(query.from_table, SubquerySource) and has_star(query.from_table.query):
            return True
        return any(isinstance(item.expression, Star) for item in query.select)

    old = original.candidate
    new = rewritten.candidate
    old_star = has_star(old.query)
    new_star = has_star(new.query)
    return (old_star and not new_star) or new.score > old.score


def sch_col_of(agg, sch):
    if not agg or agg[1] is None:
        return None
    return next((c for c in sch if c["table"] == agg[1] and c["name"] == agg[2]), None)
