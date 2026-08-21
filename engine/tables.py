"""Multi-table interpretable text->SQL over uploaded CSVs. N tables joined on DETERMINISTIC foreign keys
(engine.relations): dedup + FK discovery give the join graph, the model only PICKS which tables/columns the
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
import json
import re
import sqlite3
from functools import wraps
from pathlib import Path

import numpy as np

from engine.config import DATA_DIR, BASE_MODEL_ID as MODEL_ID  # noqa: F401 - public compatibility export
from engine.fk_edges import edges
from engine.relations import relate

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
        float(str(v).replace(",", "").lstrip("$").rstrip("%")); return True
    except ValueError:
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
        return int(v) if v.lstrip("-").isdigit() else float(v)
    except ValueError:
        return v


def table_name(name, index=0):
    """Return the canonical SQL/planner name for an uploaded or saved table."""
    value = re.sub(r"\.csv$", "", (name or "").strip(), flags=re.I)
    value = re.sub(r"[^0-9A-Za-z_]+", "_", value).strip("_").lower()
    return value or f"t{index}"


def _unquote(s):
    s = (s or "").strip() if isinstance(s, str) else s
    if isinstance(s, str) and len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def parse_rows(text, fmt="auto"):
    text = (text or "").strip()
    if fmt == "ndjson" or (fmt == "auto" and text[:1] in "{["):
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                    if isinstance(r, dict):
                        rows.append(r)
                except Exception:
                    pass
        return rows
    g = list(_csv.reader(io.StringIO(text), skipinitialspace=True))
    if len(g) < 1:
        return []
    header = [_unquote(h) or f"col{i}" for i, h in enumerate(g[0])]
    rows = []
    for r in g[1:]:
        if not any((c or "").strip() for c in r):
            continue
        rows.append({k: _typed(_unquote(v)) for k, v in zip(header, r)})
    return rows


def csv_table(csv_text, name):
    rows = parse_rows(csv_text, "auto")
    cols = list(rows[0].keys()) if rows else []
    return {"name": name, "columns": cols, "rows": [[r.get(c) for c in cols] for r in rows]}


def table_from_rows(name, columns, rows):
    """Build a planner table from structured rows using the SAME cell typing as CSV uploads (parse_rows):
    _unquote then _typed. Without this, a saved-reference cell keeps any wrapping quotes while the same value
    uploaded as CSV is unquoted, so master.relevant_tables' case-sensitive value-inclusion guard would drop the
    reference. Kept byte-identical so a master table is just another own-data table to the planner."""
    cols = [_unquote(str(column)) or f"col{i}" for i, column in enumerate(columns or [])]
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

    @staticmethod
    def _is_id(name):
        """Structural surrogate-key test (a primary/foreign key is never a SUM/AVG measure). Defined on the
        BASE so EVERY TableQuery subclass has it — the PG own-data planner (_TableQueryPg) and the offline
        EncoderQuery both call self._is_id during typed-AST search; a subclass-only definition crashed
        _TableQueryPg."""
        return bool(re.search(r"(^id$|_?id$|^index$|^pk$)", name.lower()))

    # ---------- encoding ----------
    @_torch_no_grad
    def _encode(self, texts):
        if self.qwen is None:
            raise RuntimeError("no encoder loaded — TableQuery must be overlaid with the trained encoder "
                               "(engine.knowledge_query.load_encoder / engine.encoder_overlay.EncoderQuery)")
        out = np.zeros((len(texts), self.hdim), np.float32)
        for i in range(0, len(texts), 64):
            chunk = texts[i:i + 64]
            enc = self.tok(chunk, return_tensors="pt", padding=True, truncation=True, max_length=MAX_LEN)
            h = self.qwen(**enc).last_hidden_state
            m = enc["attention_mask"].unsqueeze(-1).float()
            out[i:i + len(chunk)] = ((h * m).sum(1) / m.sum(1).clamp(min=1.0)).float().numpy()
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
        """Normalize tables, deduplicate rows, and merge trusted internal edges with discovered FKs."""
        norm = []
        for t in tables:
            cols = list(t["columns"])
            rows = [r if isinstance(r, list) else [r.get(c) for c in cols] for r in t["rows"]]
            norm.append({"name": normalize_table_name(t["name"]), "columns": cols, "rows": rows})
        g = relate(norm, explicit_fks=explicit_fks)                # dedup + trusted/discovered FK graph
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
    def _pick_join(self, involved, fks, tables):
        """If >=2 involved tables share a discovered FK, return (fact, dim, fk_col, pk_col). Else None."""
        names = set(involved) if len(involved) >= 2 else set()
        for f in fks:
            if "from_col" not in f:
                continue
            if f["from_table"] in names and f["to_table"] in names:
                return (f["from_table"], f["to_table"], f["from_col"], f["to_col"])
        if len(involved) >= 2:                                    # involved tables with no direct FK: use any FK touching one
            for f in fks:
                if "from_col" not in f:
                    continue
                if f["from_table"] in involved or f["to_table"] in involved:
                    return (f["from_table"], f["to_table"], f["from_col"], f["to_col"])
        return None

    def ast_semantic_signals(self, question, sch):
        """Encode role-specific question phrases in the same metric space as schema columns."""
        from engine.sql_rank import SemanticSignals, semantic_role_phrases
        phrases = semantic_role_phrases(question)
        columns = [c for c in sch if c.get("qvec") is not None]
        if not columns or getattr(self, "qwen", None) is None:
            return SemanticSignals.empty()
        tables = sorted({c["table"] for c in sch})
        roles = list(phrases)
        vectors = self._encode([phrases[role] for role in roles] + tables)
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
        return SemanticSignals(column_roles, table_global)

    def search_ast(self, question, sch, tables, fks, beam_size=64, max_candidates=25,
                   use_semantic_signals=True, rank_candidates=True, expand_recursive=True,
                   expand_constraints=True, expand_extrema=True):
        """Return ranked, typed SQL AST candidates from the deterministic planner.

        Bounded typed-AST search with hand-written, inspectable ranking — no trained proposer or learned
        ranker. ``tables`` stays in the signature to make the boundary explicit; the rich ``sch`` already
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

    def _serve_ast(self, question, norm, fks, sch, tablemap):
        # The pure DETERMINISTIC, fully-interpretable AST planner: bounded typed-AST search + hand-written
        # inspectable ranking, no trained proposer / learned ranker. This is the one and only own-data planner.
        candidates = self.search_ast(
            question, sch, norm, fks,
            max_candidates=25,
            use_semantic_signals=True,
        )
        if not candidates:
            return None, None, "planner: no valid AST candidate", ()
        candidate = candidates[0]
        ok, why = self.guard(candidate.sql)
        if not ok:
            return candidate, None, "guard: " + why, candidates
        try:
            cols, rows = self.execute(tablemap, sch, candidate.sql)
            result = {
                "columns": cols,
                "rows": [["" if value is None else value for value in row] for row in rows[:50]],
            }
            return candidate, result, None, candidates
        except Exception as exc:  # execution errors are returned in the serving envelope
            return candidate, None, f"{type(exc).__name__}: {exc}", candidates

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

    def execute(self, tablemap, sch, sql):
        con = sqlite3.connect(":memory:")
        by_t = {}
        for c in sch:
            by_t.setdefault(c["table"], []).append(c)
        for tname, cols in by_t.items():
            con.execute(f"CREATE TABLE {qident(tname)} (" + ", ".join(f'{qident(c["name"])} {c["affinity"]}' for c in cols) + ")")

            def coerce(v, aff):
                if v is None:
                    return None
                if aff in ("INTEGER", "REAL"):
                    try:
                        return int(float(str(v).replace(",", ""))) if aff == "INTEGER" else float(str(v).replace(",", ""))
                    except ValueError:
                        return None
                return str(v)
            t = tablemap[tname]
            ins = f"INSERT INTO {qident(tname)} VALUES ({','.join('?' * len(cols))})"
            for r in t["rows"]:
                rd = dict(zip(t["columns"], r))
                con.execute(ins, [coerce(rd.get(c["name"]), c["affinity"]) for c in cols])
        cur = con.execute(sql)
        return [d[0] for d in cur.description], cur.fetchall()

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
        from engine.sql_rank import unmet_requirements

        norm, fks = self.ingest(tables, explicit_fks=explicit_fks)
        sch, colidx, tablemap = self.schema(norm, fks)
        try:
            candidate, result, err, candidates = self._serve_ast(
                question, norm, fks, sch, tablemap
            )
        except Exception as exc:
            candidate, result, candidates = None, None, ()
            err = f"{type(exc).__name__}: {exc}"
        sql = candidate.sql if candidate is not None else None
        return {
            "question": question,
            "sql": sql,
            "valid": candidate is not None and err is None,
            "error": err,
            "result": result,
            # Hard requirements the question stated that NO candidate could satisfy. The number below
            # answers a different question than the one asked, so serving declines on this rather
            # than presenting it. Empty for every question that states no requirement.
            "unmet": [{"name": requirement.name, "detail": requirement.detail,
                       "requested": requirement.requested,
                       "available": list(requirement.available),
                       "proposal": requirement.proposal}
                      for requirement in unmet_requirements(candidates)],
            "tables": [{
                "name": table["name"], "columns": table["columns"],
                "n_rows": len(table["rows"]), "dropped": table.get("_dedup_dropped", 0),
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
            "model": "engine - deterministic typed SQL AST planner",
        }


def sch_col_of(agg, sch):
    if not agg or agg[1] is None:
        return None
    return next((c for c in sch if c["table"] == agg[1] and c["name"] == agg[2]), None)
