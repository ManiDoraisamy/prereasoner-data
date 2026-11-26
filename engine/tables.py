"""The request's tables and the own-data answer over them.

A request table is {name, columns, rows} as the planner types it, plus its ``grid``: the sheet as the own-data path reads
it (``table_grid``). ``TableQuery.select_query`` is the one own-data selection: the request's grids, read as one
workbook, answered by the Decisions path (engine/program/answer.py: closed questions over options the engine built, the
decoder's programs, one choice), every candidate a typed SQL AST (engine/program/lowering.py) run on the request's SQLite
database (engine/prepared.py). ``TableQuery.serve`` runs the chosen one and builds the response; the foreign keys
(engine.relations), the schema records and the per-layer readout (``analyze``) describe the upload to the world routes
and the /dimension view.

TableQuery does NOT load its own encoder: in the serving closure it is always composed under the unified
encoder overlay (engine.knowledge_query.load_encoder shares device/alloc/nc/dims/sid/thr/model/nL/tok/qwen/hdim onto
it), so the ONE trained model drives every path.
"""
from __future__ import annotations
import csv as _csv
import io
import re
import threading
from functools import wraps
from pathlib import Path

import numpy as np

from engine import request_timing
from engine.config import DATA_DIR, BASE_MODEL_ID as MODEL_ID  # noqa: F401 - public compatibility export
from engine.fk_edges import edges
from engine.numeric import parse_decimal, wire_decimal, observed_numeric_affinity, stored_as_numbers, has_integer_padding
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
    """Return the canonical table/row representation used by planning and execution: each table with its grid, the
    one an uploaded sheet carries, else its header and rows (``table_grid``)."""
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
            "grid": table["grid"] if "grid" in table else table_grid(table["name"], header=original_columns, rows=rows),
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
# The planner's reply when no candidate reads the question (engine/answer_presentation.UNREAD_REPLY).
NO_CANDIDATE = "planner: no valid AST candidate"
PROGRAM_MODEL = "engine - Decisions path: closed questions over the sheet's programs + typed SQL AST"


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
    if v in ("", None) or not isinstance(v, str) or has_integer_padding(v):
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


_PLAIN_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def sheet_cell(value):
    """A cell a sheet holds as text, as the sheet compiler is given it (engine/sheet): a plain number is a number, and
    every other text ("$1,234.50", "25%", "1,370 tons", a date, a name) stays as written for the sheet's own reader,
    which reads its number, unit, percent or day. A cell that is not text is kept. Typed as parse_rows types a cell
    (_typed), "25%" would be 25 and "$1,234.50" a bare amount; left all text, a column of small whole numbers reads
    as labels and the first line of figures joins the header."""
    if isinstance(value, str) and _PLAIN_NUMBER.fullmatch(value.strip()):
        return float(value)
    return value


def table_grid(name, csv_text=None, header=None, rows=None):
    """The sheet the own-data path reads for one request table (engine/program/answer.py), as the sheet compiler's wire
    form of a tab (engine/sheet/payload.py), named as the table is: {"name", "rows"}. A table's own rows are not it:
    parse_rows drops blank rows and canonicalizes the header, which destroys the structure the compiler reads.

    An uploaded sheet (``csv_text``): its CSV parsed as plain rows, blank rows kept, each cell unquoted as parse_rows
    unquotes it and given as ``sheet_cell`` gives a text cell, the trailing all-blank columns trimmed as parse_rows
    trims them. A table built from rows (``header`` and ``rows``: a saved reference, an enrichment table, a Spider
    database table, a host's rows): its header as written and its rows, each text cell as ``sheet_cell`` gives it: what
    ``normalize_tables`` gives a table that carries no grid. A unit the conversation states for a column (dataset
    semantics) rides on the grid as its "units" (engine/sheet/payload.py), never as a column the reading sees."""
    if csv_text is None:
        return {"name": str(name), "rows": [list(header), *[[sheet_cell(cell) for cell in row] for row in rows]]}
    cells = list(_csv.reader(io.StringIO(csv_text or ""), skipinitialspace=True))
    width = max((index + 1 for row in cells for index, value in enumerate(row) if value.strip()), default=0)
    return {"name": str(name), "rows": [[sheet_cell(_unquote(value)) for value in row[:width]] for row in cells]}


def csv_table(csv_text, name):
    rows = parse_rows(csv_text)
    cols = list(rows[0].keys()) if rows else []
    return {"name": name, "columns": cols, "rows": [[r.get(c) for c in cols] for r in rows],
            "grid": table_grid(name, csv_text)}


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
        values), the global column index, and the tables by name. Built once for equal tables
        (`relations.memoized`): each record and mapping is the caller's own copy."""
        from engine.relations import content_key, memoized, table_cells
        sch, colidx, tablemap = memoized(
            "planner_schema", lambda: (id(self.model), content_key(tables), repr(fks)),
            lambda: self._schema(tables, fks), table_cells(tables))
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
    def select_query(self, question, tables):
        """Choose the query to serve for an own-data question over the request's ``tables``; returns a
        ``PoolSelection`` (engine/sql_rank.py). This is the one own-data selection: serving, the compose probe,
        decomposition leaves and the evaluators (the Spider evaluator, the messy benchmark, the dev replay) call it.

        The tables are one workbook, a tab per table in their order, each its ``grid`` (``table_grid``), prepared once
        per content (engine/prepared.py prepared_workbook), with the stock reading's vectors its conversation keeps for
        its tables (engine/prepared.py hold_sheet_vectors). In a served request the world layer's links connect it
        (engine/program/connected.py, engine/prepared.py connected_workbook): each place or entity column gains the
        knowledgebase columns it reaches, each key the other table's columns, and, when the convert question names the
        currency the question wants (engine/program/answer.py convert_target), each money column its amount in that
        currency. The Decisions path answers the question on the request's
        database (engine/program/answer.py), once per request (RequestDatabase.answered), naming the tab the client has
        in view (RequestDatabase.active_sheet): the stock reading and one closed-question call read the question, the
        decoder builds the programs, the verifier runs each one, one choice call weighs those that returned rows, and
        the likeliest answer is answered, warned or most likely wrong (engine/program/closed.py ERRED: nothing is
        decided, and ``erred`` holds it). No model writes a program; the retry of an erred answer (``serve``,
        engine/retry.py) is the one written query. The candidates that returned rows are the pool, each its program's
        typed AST (engine/program/lowering.py) and SQL with its read-as line as evidence. A question longer than
        ONE_QUERY_WORDS words whose answer is most likely wrong asks, in an analysis, for the decomposition proposal
        (engine/decomposition.py declined_compound). Raises LLMUnavailable when a Decisions call fails."""
        from engine.prepared import hold_sheet_vectors, scratch_database
        from engine.program.answer import ERRED, WARNED, answer
        from engine.program.closed import answer_masses
        from engine.program.lowering import to_query
        from engine.sql_ast import render_query
        from engine.sql_candidate import ScoredQuery
        from engine.sql_rank import PoolSelection

        with scratch_database() as database:
            key, prepared, targets = self.workbook(question, tables, database)
            hold_sheet_vectors(tables, database)
            store = database.program_tables(key, prepared.catalog)
            said = database.answered(key, question,
                                     lambda: answer(question, prepared, store, database.active_sheet))
        mass, _first = answer_masses(said.probabilities, said.listed_answers)
        scores = [mass.get(answer_id, 0.0) for answer_id in said.listed_answers]
        pool = []
        for program, line, score in zip(said.listed_programs, said.listed_read_as, scores):
            query = to_query(program, prepared.catalog)
            pool.append(ScoredQuery(query, render_query(query), score, (line,)))
        ranking = tuple(sorted(range(len(pool)), key=lambda index: (-scores[index], index)))
        erred = said.kind == ERRED
        return PoolSelection(tuple(pool), ranking, None if erred else said.pick, said.mass,
                             said.pick if erred else None, said.kind == WARNED, said.uncertain, said.runner_up,
                             said.probabilities, said.listed_programs, said.listed_rows, prepared.catalog, store, bool(targets),
                             said.alike, said.doubt)

    @staticmethod
    def workbook(question, tables, database):
        """(key, preparation, conversion targets) of the workbook ``question`` reads of the request's ``tables`` on the
        request's ``database``: compiled once per content (engine/prepared.py prepared_workbook); in a served request
        connected by its links (what its place and entity columns reach in the knowledgebase, the keys between its
        tables: connected_workbook), and, when the path's convert question names the currency the question wants its
        answer in (engine/program/answer.py convert_target), each money column read in that currency. The Decisions
        path (``select_query``) and the retry (``serve``, ``retry_answer``) read this one workbook."""
        from engine.prepared import connected_workbook, prepared_workbook
        from engine.program.answer import convert_target
        from engine.program.connected import conversion_targets

        key, prepared = prepared_workbook(tables)
        links = database.links(key, tables)
        targets = ()
        if links is not None:
            own_key, own = key, prepared
            key, prepared = database.connected(own_key, None, lambda: connected_workbook(own_key, own, links, database.connection))
            targets = conversion_targets(links)
            if targets:
                catalog = prepared.catalog
                target = database.answered(("convert", key), question,
                                           lambda: convert_target(question, catalog, targets, database.active_sheet))
                if target is not None:
                    key, prepared = database.connected(own_key, target,
                                                       lambda: connected_workbook(own_key, own, links, database.connection, target))
        return key, prepared, targets

    def _serve_ast(self, question, norm, tablemap):
        """Select the own-data query (``select_query``) and run it. In an analysis, as every served request is, the
        chosen program's one plan runs through both deterministic emitters on the request's database
        (``_execute_deterministic``, which records it); outside one (an evaluator, a test) the answer is the rows the
        choice read. Returns (candidate, result, error, selection); a question whose answer is most likely wrong has
        neither a candidate nor an error."""
        selection = self.select_query(question, norm)
        if selection.candidate is None:
            return None, None, None, selection
        candidate, result, err = self._run_member(selection, selection.selected, tablemap)
        return candidate, result, err, selection

    def _run_member(self, selection, index, tablemap):
        """Run pool member ``index`` of ``selection`` as ``_serve_ast`` runs the decided one: (candidate, result,
        error)."""
        from engine.deterministic.context import current_analysis_context

        candidate = selection.pool[index]
        ok, why = self.guard(candidate.sql)
        if not ok:
            return candidate, None, "guard: " + why
        context = current_analysis_context()
        try:
            if context is None:
                rows = selection.answer(index)
            else:
                _columns, rows = self._execute_deterministic(
                    tablemap, selection.plan(index, context.slug), estimated_rows=selection.estimated_rows(index))
                rows = selection.shown(index, rows)
        except Exception as exc:  # execution errors are returned in the serving envelope
            return candidate, None, f"{type(exc).__name__}: {exc}"
        result = {"columns": list(selection.columns(index)),
                  "rows": [["" if value is None else value for value in row] for row in rows]}
        return candidate, result, None

    def retry_answer(self, tables, question, explicit_fks=()):
        """The person's retry of an answer (engine/retry.py): Gemini 4 Neon writes the query over the workbook the
        question reads (``workbook``: its tables and what the request's links reach), whatever the Decisions path read. The response has ``serve``'s shape: the retry's rows and query (``check``
        retried, asked), its word that the sheet cannot answer the question (the one question back), or the error its
        query met."""
        from engine.prepared import scratch_database
        from engine.query_contract import Coverage
        from engine.retry import retry, workbook_tables

        norm, _fks = self.ingest(tables, explicit_fks=explicit_fks)
        with scratch_database() as database:
            _key, prepared, _targets = self.workbook(question, norm, database)
            retried = retry(question, workbook_tables(prepared.catalog), self.guard)
        response = {"question": question, "sql": retried.sql or None, "valid": retried.answered, "error": None,
                    "result": None, "read_as": retried.reason or None, "model": retried_model(),
                    "tables": [{"name": table["name"], "columns": table["columns"], "n_rows": len(table["rows"])}
                               for table in norm]}
        if retried.answered:
            response.update(result=retried_result(retried), check=retried_check(retried, None, None, asked=True),
                            coverage=Coverage(True).record())
        elif not retried.answerable:
            response.update({"clarify": True, "reason": retried.reason or UNANSWERABLE, "dropped": [],
                             "retry": retried.record()})
        else:
            response.update(error=RETRY_FAILED, check={"state": "erred", "confidence": None, "reason": RETRY_FAILED,
                                                       "retry": retried.record()})
        return response

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

    def _execute_deterministic(self, tablemap, plan, knowledgebase_release=None, labels=None, estimated_rows=None):
        """Run a served plan through both emitters on the request's database. ``labels`` resolves knowledgebase
        entities to their labels for display (docs/SHEETS_AS_REASONING.md rule 5); a plan that reads the
        knowledgebase cannot be shown without it, so every world trail passes it here, the one executor they
        share. ``estimated_rows``: the rows the plan reads, when its tables are not the request tables of
        ``tablemap`` (the own-data path's, engine/program/store.py)."""
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        from engine.analysis import analysis_input_hash
        from engine.config import (APP_ENV, deterministic_execution_mode, deterministic_persist_generated,
                                   deterministic_python_row_limit)
        from engine.deterministic.context import current_analysis_context, set_execution_record
        from engine.deterministic.service import DeterministicAnalysis
        from engine.prepared import SharedConnection, current_database

        context = current_analysis_context()
        if context is None:
            raise RuntimeError("deterministic execution requires a named-analysis context")
        if labels is None and any(table.schema == "knowledgebase" for table in plan.tables):
            raise RuntimeError("a plan that reads the knowledgebase needs its entity labels")
        database = current_database()
        if database is None:
            raise RuntimeError("deterministic execution runs on the request's database")
        shared = SharedConnection(database.connection)
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
            estimated_rows=estimated_rows if estimated_rows is not None else sum(
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
        execution_record = result.record()
        if result.source_lineage is not None:
            execution_record["source_lineage"]["input_order"] = database.source_row_snapshots(tablemap)
        set_execution_record(execution_record)
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
        """tables: [{name, columns, rows, grid}]. The own-data answer to ``question`` (``select_query``), run on the
        request's database, as the response the web UI, the world layer's delegate and the evaluators read."""
        from engine.llm import LLMUnavailable
        from engine.prepared import scratch_database
        from engine.sql_schema import SchemaGraph
        norm, fks = self.ingest(tables, explicit_fks=explicit_fks)
        sch, colidx, tablemap = self.schema(norm, fks)
        graph = SchemaGraph.from_planner(sch, fks)
        clarification = declined = retried = check = None
        with scratch_database():
            try:
                candidate, result, err, selection = self._serve_ast(question, norm, tablemap)
            except LLMUnavailable:
                raise                                # the path does not answer from a reading the API did not make
            except Exception as exc:
                candidate, result, selection = None, None, None
                err = f"{type(exc).__name__}: {exc}"
            if candidate is None and err is None:
                # The likeliest answer is most likely wrong (engine/program/closed.py ERRED): a long question asks, in
                # an analysis, for its decomposition; any other is retried (engine/retry.py), never asked back. The
                # retry's query answers it; the retry's word that the sheet cannot answer it is the one question
                # back (the question is wrong); a retry that cannot answer leaves the likeliest reading served,
                # marked as most likely wrong.
                from engine.decomposition import declined_compound
                from engine.deterministic.context import current_analysis_context
                from engine.retry import retry, workbook_tables

                if current_analysis_context() is not None:
                    declined = declined_compound(selection, question)
                if declined is None:
                    retried = retry(question, workbook_tables(selection.catalog), self.guard)
                    likeliest = selection.pool[selection.erred] if selection.erred is not None else None
                    if retried.answered:
                        result = retried_result(retried)
                        check = retried_check(retried, read_as_of(likeliest), selection.confidence, asked=False)
                    elif not retried.answerable:
                        clarification = {"clarify": True, "reason": retried.reason or UNANSWERABLE, "dropped": [],
                                         "read_as": read_as_of(likeliest), "retry": retried.record()}
                    elif likeliest is not None:
                        candidate, result, err = self._run_member(selection, selection.erred, tablemap)
                        check = {"state": "erred", "confidence": _rounded(selection.confidence),
                                 "reason": erred_reason(read_as_of(likeliest), selection.confidence, True),
                                 "retry": retried.record()}
                    else:
                        err = NO_CANDIDATE
                        check = {"state": "erred", "confidence": None, "reason": erred_reason(None, None, True),
                                 "retry": retried.record()}
        sql = candidate.sql if candidate is not None else (retried.sql if retried is not None and retried.answered else None)
        computation = None
        if candidate is not None:
            from engine.calculations import describe_computation
            computation = describe_computation(candidate.query)
        response = {
            "question": question,
            "sql": sql,
            "valid": result is not None and err is None,
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
            "candidate_count": len(selection.pool) if selection is not None else 0,
            "evidence": list(candidate.evidence) if candidate is not None else [],
            "features": {},
            "computation": computation.record() if computation is not None else None,
            "selection": selection.record() if selection is not None else None,
            "read_as": read_as_of(candidate) if candidate is not None else (retried.reason if check and check["state"] == "retried" else None),
            "model": PROGRAM_MODEL if check is None or check["state"] != "retried" else retried_model(),
        }
        if clarification is not None:
            response.update(clarification)
        if check is not None:
            response["check"] = check
            if check["state"] == "retried":
                from engine.query_contract import Coverage
                response["coverage"] = Coverage(True).record()     # the retry's query read the whole question
        if candidate is not None:
            # The Decisions path chose this reading of the whole question: it is the own-data planner's certificate
            # (engine/knowledge_query.py keeps its residual wording gate for the routes without one).
            from engine.query_contract import Coverage
            response['coverage'] = Coverage(True).record()
            if check is None:
                response["check"] = answer_check(selection, candidate)
            from engine.sql_ast import share_output
            if share_output(candidate.query):
                response["unit"] = "percent"         # a share of a whole: 0.3 is stated as 30%
            # The Decisions path's reading is its own certificate for the calculation it chose: the registry
            # (engine/calculations) reads the request's schema and the path's programs read its store tables, so the
            # registry can never find a ratio, a rate or a currency filter in this query, and its gate would ask back
            # every answer it detects one in. What the reading cannot certify is a conversion the path had no way to
            # make: a currency the question wants its answer in when no money column of the request can be read in
            # another (``converts``); the registry's currency assessment keeps that answer from being served unconverted.
            # The registry reads the query's columns as the request names them (request_named). A currency the
            # program keeps rows by ("how many orders in EUR") is recorded when the program realizes it; it is
            # evidence, never a reason to ask back.
            from engine.calculations import assess_calculations
            from engine.calculations.registry import attach_calculation_evidence
            assessed = assess_calculations(question, norm, graph, request_named(computation, selection.catalog))
            attach_calculation_evidence(response, tuple(
                row for row in assessed if row.get("specification") == "currency"
                and ((row.get("operation") == "convert" and not selection.converts)
                     or (row.get("operation") == "filter" and row.get("status") == "satisfied"))))
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
        return response


def request_named(evidence, catalog):
    """``evidence`` (engine/calculations describe_computation of a Decisions-path query, which reads the request's store
    tables) with each store column named as the request names it (engine/program/store.py request_name): the
    calculation registry reads the request's own schema, so it reads the measure and the currency column it finds
    beside it as the request's columns, not as c1_n on _t0."""
    from dataclasses import fields, is_dataclass, replace

    from engine.calculations.core import PredicateFact
    from engine.program.store import request_name
    from engine.sql_ast import ColumnRef

    def renamed(node):
        if isinstance(node, ColumnRef):
            table, name = request_name(catalog, node.table, node.name)
            return replace(node, table=table, name=name)
        if isinstance(node, PredicateFact):
            table, column = request_name(catalog, node.table, node.column)
            return replace(node, table=table, column=column)
        if isinstance(node, (tuple, list, frozenset)):
            return type(node)(renamed(item) for item in node)
        if is_dataclass(node) and not isinstance(node, type):
            return replace(node, **{field.name: renamed(getattr(node, field.name)) for field in fields(node) if field.init})
        return node

    return evidence if evidence is None or catalog is None else renamed(evidence)


def read_as_of(candidate):
    """The own-data path's plain reading of ``candidate`` (engine/program/output.py read_as), its first evidence, or
    None."""
    evidence = getattr(candidate, "evidence", None) or ()
    first = evidence[0] if evidence else None
    return first if isinstance(first, str) and first else None


UNANSWERABLE = "The sheet does not hold what this question asks."
RETRY_FAILED = "The retry could not write a query that runs on this sheet."


def _rounded(value):
    return None if value is None else round(float(value), 4)


def answer_check(selection, candidate):
    """The response's ``check`` of a decided answer: answered, or warned (``PoolSelection.warned``: below
    closed.T_FLAG, another answer's reading reads word for word like it, or the second judge did not pick it) with the
    reason its mark opens on (how it was read, how sure the choice was, what else the question may mean: the runner-up's
    reading, the second judge's own pick when it picked another answer) and the coordinates the runner-up reads
    differently (engine/program/language.coordinates)."""
    if not selection.warned:
        return {"state": "answered", "confidence": _rounded(selection.confidence)}
    other = selection.runner_up
    also = read_as_of(selection.pool[other]) if other is not None else None
    from engine.answer_presentation import warned_reason
    return {"state": "warned", "confidence": _rounded(selection.confidence),
            "reason": warned_reason(read_as_of(candidate), also, selection.confidence, selection.alike, selection.doubt),
            "coordinates": list(selection.uncertain), "also_read_as": also}


def erred_reason(read_as, confidence, retried):
    from engine.answer_presentation import erred_reason as reason
    return reason(read_as, confidence, retried)


def retried_model():
    from engine import config
    return f"{config.QUERY_MODEL} (retry: a written SQLite query)"


def retried_result(retried):
    """A retry's rows as the response's result."""
    return {"columns": list(retried.columns), "rows": [["" if value is None else value for value in row] for row in retried.rows]}


def retried_check(retried, read_as, confidence, asked):
    """The ``check`` of an answer the retry wrote (engine/retry.py): the person asked for it, or the path's likeliest
    reading (``read_as``, ``confidence``) was most likely wrong; its reason, and the retry's own record."""
    from engine.answer_presentation import retried_reason
    return {"state": "retried", "confidence": _rounded(confidence), "asked": asked,
            "reason": retried_reason(read_as, confidence, retried.reason, asked), "retry": retried.record()}


def sch_col_of(agg, sch):
    if not agg or agg[1] is None:
        return None
    return next((c for c in sch if c["table"] == agg[1] and c["name"] == agg[2]), None)
