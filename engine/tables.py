"""Multi-table interpretable text->SQL over uploaded CSVs. N tables joined on DETERMINISTIC foreign keys
(engine.relations): dedup + FK discovery give the join graph, the model only PICKS which tables/columns the
question refers to (learned intent + name/representation binding) and the SQL is assembled with qualified
`"t"."c"` identifiers, guarded SELECT-only, and executed on an in-memory SQLite with every table created.
Per-token readout is read PER LAYER through the SAME anchored model, so a JOIN's `"orders"."customer_id"`
fires is_field + the FK edge to `"customers"."customer_id"`.

TableQuery does NOT load its own encoder: in the serving closure it is always composed under the unified
encoder overlay (engine.world_query.load_encoder shares alloc/nc/dims/sid/thr/model/nL/tok/qwen/hdim onto
it), so the ONE trained model drives every path.
"""
from __future__ import annotations
import csv as _csv
import io
import json
import re
import sqlite3
from pathlib import Path

import numpy as np
import torch

from engine.config import DATA_DIR, BASE_MODEL_ID as MODEL_ID
from engine.fk_edges import edges
from engine.relations import relate

MAX_ROWS, MAX_LEN = 12, 48
FORBID = re.compile(r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|replace|truncate|vacuum|with)\b", re.I)


def qident(s):
    return '"' + str(s).replace('"', '""') + '"'


def qlit(s):
    return "'" + str(s).replace("'", "''") + "'"


def qual(t, c):
    return f'{qident(t)}.{qident(c)}'


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


class TableQuery:
    def __init__(self, deploy_dir=DATA_DIR):
        # DEFERRED encoder: the serving closure never runs TableQuery standalone — engine.world_query's
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
        self._ast_proposal_provider = None
        self._ast_runtime_models = {}

    @staticmethod
    def _is_id(name):
        """Structural surrogate-key test (a primary/foreign key is never a SUM/AVG measure). Defined on the
        BASE so EVERY TableQuery subclass has it — the PG own-data planner (_TableQueryPg) and the offline
        EncoderQuery both call self._is_id from plan(); a subclass-only definition crashed _TableQueryPg."""
        return bool(re.search(r"(^id$|_?id$|^index$|^pk$)", name.lower()))

    # ---------- encoding ----------
    @torch.no_grad()
    def _encode(self, texts):
        if self.qwen is None:
            raise RuntimeError("no encoder loaded — TableQuery must be overlaid with the trained encoder "
                               "(engine.world_query.load_encoder / engine.encoder_overlay.EncoderQuery)")
        out = np.zeros((len(texts), self.hdim), np.float32)
        for i in range(0, len(texts), 64):
            chunk = texts[i:i + 64]
            enc = self.tok(chunk, return_tensors="pt", padding=True, truncation=True, max_length=MAX_LEN)
            h = self.qwen(**enc).last_hidden_state
            m = enc["attention_mask"].unsqueeze(-1).float()
            out[i:i + len(chunk)] = ((h * m).sum(1) / m.sum(1).clamp(min=1.0)).float().numpy()
        return out

    @torch.no_grad()
    def _layers(self, units, x):
        E = edges(units); S = len(units)
        outs = self.model.forward_layers(torch.from_numpy(x)[None], torch.from_numpy(E)[None],
                                         torch.zeros(1, S, dtype=torch.bool))
        return [o[0].detach().numpy() for o in outs]

    def _fires(self, vec, fam, thr=0.5):
        return [d["name"].split("_", 1)[1] for d in self.dims
                if d["family"] == fam and vec[d["dim_id"]] >= self.thr.get(d["name"], thr)]

    # ---------- ingest + schema ----------
    def ingest(self, tables):
        """tables: [{name, columns, rows(list[dict] or list[list])}]. Dedup + discover FKs (deterministic)."""
        norm = []
        for t in tables:
            cols = list(t["columns"])
            rows = [r if isinstance(r, list) else [r.get(c) for c in cols] for r in t["rows"]]
            norm.append({"name": re.sub(r"\W+", "_", str(t["name"])).strip("_") or "t", "columns": cols, "rows": rows})
        g = relate(norm)                                          # dedup + FK discovery
        return g["tables"], g["fks"]

    def _schema_name_units(self, tables, fks):
        """One name-only schema unit per (table, column), GLOBAL col index, ref=(to_table,to_col) on FK cols."""
        ref_of = {(f["from_table"], f["from_col"]): (f["to_table"], f["to_col"]) for f in fks}
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
    def plan(self, question, sch, tables, fks):
        toks = question.split(); low = [t.lower() for t in toks]
        units, colidx = self._schema_name_units(tables, fks)
        qstart = len(units)
        units += [{"text": t, "group": "q", "kind": "q", "table": None, "col": -1, "colname": None, "row": -2} for t in toks]
        x = self._encode([u["text"] for u in units])
        final = self._layers(units, x)[-1]
        qvec = x[qstart:]
        numeric = [c for c in sch if c["affinity"] in ("INTEGER", "REAL")]
        operand = set()
        for c in sch:
            n = c["name"].lower(); operand.add(n); operand.update(n.split())
        operand |= {str(v).lower() for c in sch for v in c["values"] if v is not None}
        AGG_CUES = {"count": {"count", "counts", "number", "numbers"}, "sum": {"sum", "sums", "total", "totals"},
                    "avg": {"avg", "average", "averages", "mean", "means"}}
        MARGIN = 0.10

        def iscore(intent):
            d = self.sid[f"intent_{intent}"]; t = self.thr.get(f"intent_{intent}", 0.5)
            cand = [i for i in range(len(toks)) if low[i] not in operand]
            if not cand:
                return 0.0, 0, False
            j = max(cand, key=lambda i: float(final[qstart + i][d]))
            sc = float(final[qstart + j][d])
            return sc, j, sc >= t

        def col_after(preps, kind="any"):
            for i, t in enumerate(low):
                if t in preps:
                    for j in range(i + 1, len(toks)):
                        c = self._link(qvec[j], low[j], sch, kind=kind)
                        if c:
                            return c
            return None

        slots = {"select": [], "where": [], "group_by": [], "order_by": [], "limit": None, "agg": None, "distinct": False}

        def stem_date():
            """a question token sharing a >=5-char prefix with a DATE column's name word pins that column
            ("subscribed" -> "Subscription Date")."""
            for c in sch:
                if c.get("is_date") and any(len(w) >= 5 and any(len(t) >= 5 and t[:5] == w[:5] for t in low)
                                            for w in name_words(c["name"])):
                    return c
            return None

        sd, _, sdf = iscore("sort_desc"); sa, _, saf = iscore("sort_asc")
        if sdf or saf:                                             # learned sort intent has NO verb gate, so only
            col = stem_date() or col_after({"by"}, kind="num")     # honor it when a sort column is actually nameable
            if col:                                                # (date phrasing or "by <col>"); never fabricate an
                slots["order_by"] = [(col, "DESC" if sd >= sa else "ASC")]  # ORDER BY on the row id when it misfires

        # RECENCY over a value-sniffed DATE column ("recently subscribed", "latest", "oldest"). The learned sort
        # intent rarely fires on these phrasings and the sort slot was numeric-only, so date recency gets its own
        # deterministic cue gate (cue word + a typed column, no blind fallback).
        REC_DESC = {"recent", "recently", "latest", "newest"}; REC_ASC = {"earliest", "oldest"}
        ri = next((i for i, t in enumerate(low) if t in REC_DESC | REC_ASC), -1)
        if ri >= 0 and not slots["order_by"]:
            dcol = stem_date() or next((c for c in sch if c.get("is_date")), None)
            if dcol:
                slots["order_by"] = [(dcol, "DESC" if low[ri] in REC_DESC else "ASC")]

        # EXPLICIT sort cue ("sort/sorted/order/ordered/arrange/ranked by <col> [ascending|descending]") — the learned
        # sort intent often misses these phrasings, so gate deterministically on the verb + the column after "by".
        if ({"sort", "sorted", "order", "ordered", "arrange", "arranged", "rank", "ranked"} & set(low)) and not slots["order_by"]:
            asc = any(t in {"ascending", "asc", "increasing", "alphabetical"} for t in low)
            desc = any(t in {"descending", "desc", "decreasing"} for t in low)
            col = col_after({"by"}, kind="any") or stem_date() or (numeric[0] if numeric else None)
            if col:
                hi = col["affinity"] in ("INTEGER", "REAL") or col.get("is_date")   # numbers/dates default high-first
                slots["order_by"] = [(col, "ASC" if asc else ("DESC" if (desc or hi) else "ASC"))]

        if iscore("limit")[2]:                                    # learned LIMIT intent: take a bare count, but NOT
            CMP = {"over", "above", "greater", "exceeds", "more", "under", "below", "less", "fewer", "cheaper",
                   "younger", "smaller", "after", "before", "since", "than", "top"}   # a number right after a
            for i, t in enumerate(low):                           # comparison cue is that filter's OPERAND ("price
                if t.isdigit() and not re.fullmatch(r"(19|20)\d{2}", t) and (i == 0 or low[i - 1] not in CMP):
                    slots["limit"] = int(t); break                # over 500" is a WHERE, not LIMIT 500), nor a year

        if "top" in low and slots["limit"] is None:               # "top 5 …" -> LIMIT 5 + a DESC order if none yet
            k = next((int(t) for t in low if t.isdigit()), None)
            if k:
                slots["limit"] = k
                if not slots["order_by"]:
                    mp = [c for c in numeric if not re.search(r"(^id$|_?id$)", c["name"].lower())]
                    col = stem_date() or col_after({"by"}, kind="num") or (mp[0] if mp else None)
                    if col:
                        slots["order_by"] = [(col, "DESC")]

        present = [nm for nm, cues in AGG_CUES.items() if any(t in cues for t in low)]
        if not present and "how" in low and "many" in low:
            present = ["count"]
        # "total NUMBER of X" is a row count — 'total' merely modifies the count noun, it is not a SUM cue.
        # Without this, both cues fire and the intent margin sometimes picks SUM -> SUM(first numeric).
        if "sum" in present and "count" in present and any(
                low[i + 1] in AGG_CUES["count"] for i, t in enumerate(low[:-1]) if t in AGG_CUES["sum"]):
            present.remove("sum")
        if present:
            scored = sorted(((nm, iscore(f"agg_{nm}")[0]) for nm in present), key=lambda a: -a[1])
            if len(scored) == 1 or scored[0][1] - scored[1][1] >= MARGIN:
                nm = scored[0][0]
                if nm == "count":
                    slots["agg"] = ("COUNT", None, "*")
                else:
                    ci = next((i for i, t in enumerate(low) if t in AGG_CUES[nm]), -1)
                    tgt = None
                    for k in range(ci + 1, len(toks)):
                        tgt = self._link(qvec[k], low[k], sch, kind="num")
                        if tgt:
                            break
                    if tgt is None and any(tk == t["name"].lower() or tk.rstrip("s") == t["name"].lower().rstrip("s")
                                           for tk in low for t in tables):
                        # "total SINGERS" names the ENTITY, not a measure: no numeric column is nameable
                        # after the cue but a TABLE is named -> row count (EncoderQuery.read_op_all's
                        # token_table precedence), instead of blindly SUM-ing the first numeric column.
                        slots["agg"] = ("COUNT", None, "*")
                    else:
                        tgt = tgt or (numeric[0] if numeric else None)
                        if tgt:
                            slots["agg"] = ("SUM" if nm == "sum" else "AVG", tgt["table"], tgt["name"])

        if not slots["agg"]:                                      # MIN/MAX agg ("maximum index", "max subscription
            MM = {"MAX": {"max", "maximum"}, "MIN": {"min", "minimum"}}   # date") — cue + an EXPLICIT numeric/date
            for fn, cues in MM.items():                                   # target after it; no blind fallback
                ci = next((i for i, t in enumerate(low) if t in cues), -1)
                if ci < 0:
                    continue
                pool = [c for c in sch if (c["affinity"] in ("INTEGER", "REAL") or c.get("is_date"))
                        and not re.search(r"(^id$|_?id$)", c["name"].lower())]   # ids are keys, not measures
                tgt = None
                for k in range(ci + 1, len(toks)):
                    tk = low[k]
                    tgt = next((c for c in pool if tk == c["name"].lower() or
                                any(wmatch(tk, w) or (len(w) >= 5 and len(tk) >= 5 and tk[:5] == w[:5])
                                    for w in name_words(c["name"]))), None)
                    if tgt:
                        break
                if tgt:
                    slots["agg"] = (fn, tgt["table"], tgt["name"]); break

        gcol = col_after({"per", "each"}, kind="cat")
        if gcol is None and "group" in low:
            gi = low.index("group")
            if gi + 1 < len(low) and low[gi + 1] == "by":
                for k in range(gi + 2, len(toks)):
                    gcol = self._link(qvec[k], low[k], sch, kind="cat")
                    if gcol:
                        break
        if gcol is None and slots["agg"]:                         # "average weight BY sex", "count BY category" ->
            gcol = col_after({"by"}, kind="cat")                  # GROUP BY (only a CATEGORICAL col after "by"; a
        if gcol is not None and (iscore("group")[2] or slots["agg"]):  # numeric "by sales" is sort, handled above)
            slots["group_by"] = [gcol]

        # eq filter: a CELL VALUE quoted in the question — PHRASE-aware (multi-word values like "United States of
        # America" match too), longest match wins; "not/except/without/excluding" just before it negates.
        qstr = " " + " ".join(low) + " "
        NEG = {"not", "except", "without", "excluding"}
        best = None
        for c in sch:
            for v in {str(v) for v in c["values"] if v is not None and str(v).strip() != ""}:
                vl = v.lower()
                if len(vl) < 2 or vl == str(slots["limit"]):      # don't re-match the LIMIT/top-K number as a cell
                    continue
                if re.fullmatch(r"-?[\d,]*\.?\d+%?", vl):         # a NUMBER is a comparison operand, not a categorical
                    continue                                      # equality ("over 5000" must not also add "= 5000")
                m = re.search(r"(?<![a-z0-9])" + re.escape(vl) + r"(?![a-z0-9])", qstr)
                if m and (best is None or len(vl) > len(best[2])):
                    neg = bool(NEG & set(qstr[:m.start()].split()[-3:]))
                    best = (c, "!=" if neg else "=", v)
        if best:
            slots["where"].append(best)
        GT_CUES = {"over", "above", "greater", "exceeds", "more"}
        LT_CUES = {"under", "below", "less", "fewer", "cheaper", "younger", "smaller"}
        for op, cues in ((">", GT_CUES), ("<", LT_CUES)):
            ci = next((i for i, t in enumerate(low) if t in cues), -1)
            if ci < 0:
                continue
            col = None
            for k in range(ci - 1, -1, -1):
                col = self._link(qvec[k], low[k], sch, kind="num")
                if col:
                    break
            col = col or (numeric[0] if numeric else None)
            val = next((toks[k].replace(",", "") for k in range(ci + 1, len(toks))
                        if re.fullmatch(r"-?\d[\d,]*\.?\d*", toks[k]) and toks[k].replace(",", "") != str(slots["limit"])), None)
            if col is not None and val is not None:
                slots["where"].append((col, op, val))

        # DATE / YEAR filter: a 4-digit year + a year-ish column. A real DATE column compares the ISO year
        # prefix (substr(col,1,4) op 'YYYY', wrap="year"); an INTEGER/REAL year column ("Year", "*_year", or a
        # column whose values are all 4-digit years) compares NUMERICALLY (Year = 1980) — "how many cars were
        # made in 1980". Without the integer arm the year token silently dropped (counted every row).
        datecols = [c for c in sch if c.get("is_date")]

        def _is_intyear(c):
            if c.get("is_date") or c["affinity"] not in ("INTEGER", "REAL") or self._is_id(c["name"]):
                return False
            n = c["name"].lower()
            if n == "year" or n.endswith("_year"):
                return True
            vs = [str(v).strip() for v in (c.get("values") or []) if v is not None and str(v).strip() != ""]
            return bool(vs) and all(re.fullmatch(r"(19|20)\d\d", v) for v in vs)

        intyearcols = [c for c in sch if _is_intyear(c)]
        years = [t for t in low if re.fullmatch(r"(19|20)\d{2}", t)]
        if (datecols or intyearcols) and years and not any(str(w[2]).lower() in years for w in slots["where"]):
            ycol = stem_date() or (datecols[0] if datecols else intyearcols[0])
            wrap = "year" if ycol.get("is_date") else None      # date -> substr prefix; int year -> plain numeric

            def _wt(op, y):
                return (ycol, op, y, wrap) if wrap else (ycol, op, y)
            if len(years) >= 2 and "between" in low:
                slots["where"] += [_wt(">=", years[0]), _wt("<=", years[1])]
            else:
                yi = low.index(years[0])
                prep = next((low[j] for j in range(yi - 1, max(-1, yi - 3), -1)
                             if low[j] in {"in", "during", "of", "before", "after", "since", "from"}), "in")
                op = {"before": "<", "after": ">", "since": ">=", "from": ">="}.get(prep, "=")
                slots["where"].append(_wt(op, years[0]))

        # numeric "after/before/since N" on a NUMERIC column that is named in the query — e.g. an integer year
        # column ("founded after 2010" -> Founded > 2010). The date filter above already handled before/after for
        # real date columns, so here: require the column to be matched by NAME (not a fuzzy rep-link — otherwise
        # "born before 1990" would link "born" to a numeric Index), and skip any year a date filter already used.
        for op, cues in ((">", {"after"}), ("<", {"before"}), (">=", {"since"})):
            ci = next((i for i, t in enumerate(low) if t in cues), -1)
            if ci < 0:
                continue
            col = None
            for k in range(ci - 1, -1, -1):
                tk = low[k]
                col = next((c for c in numeric if tk == c["name"].lower()
                            or any(wmatch(tk, w) for w in name_words(c["name"]))), None)
                if col:
                    break
            val = next((toks[k].replace(",", "") for k in range(ci + 1, len(toks))
                        if re.fullmatch(r"-?\d[\d,]*\.?\d*", toks[k])), None)
            if col is not None and val is not None and not any(str(w[2]) == val for w in slots["where"] if len(w) >= 3):
                slots["where"].append((col, op, val))

        # SUPERLATIVE argmax — "X who/that <verb> the most/least", "X with the most <measure>". There is no
        # explicit "top K"/agg CUE word, so the cue-gated agg/sort above won't fire; build GROUP BY the entity
        # + SUM(measure) + ORDER BY that aggregate + LIMIT 1, joining the entity (dim) to the fact that holds
        # the measure. "the most" = argmax, so exactly one row.
        SUP_DESC = {"most", "highest", "largest", "biggest", "greatest", "maximum", "max", "best"}
        SUP_ASC = {"least", "lowest", "smallest", "fewest", "minimum", "min", "worst"}
        si = next((i for i, t in enumerate(low) if t in SUP_DESC or t in SUP_ASC), -1)
        if si >= 0 and slots["limit"] is None and not slots["agg"]:
            sdir = "DESC" if low[si] in SUP_DESC else "ASC"
            # a real MEASURE is a numeric column that is NOT a key/id — the thing you'd actually SUM. Search for a
            # measure NAMED after the superlative ONLY against this pool, so "order amount" can't grab `order_id`.
            meas_pool = [c for c in sch if c["affinity"] in ("INTEGER", "REAL") and not re.search(r"(^id$|_?id$)", c["name"].lower())]
            mcol = None
            for k in range(si + 1, len(toks)):
                mcol = self._link(qvec[k], low[k], meas_pool, kind="num")
                if mcol:
                    break
            named_t = {t["name"] for t in tables
                       if any(tk == t["name"].lower() or tk.rstrip("s") == t["name"].lower().rstrip("s") for tk in low)}
            argmax = None
            for f in fks:                                         # entity named + FK to a fact that has a measure
                meas = [c for c in meas_pool if c["table"] == f["from_table"] and c["name"] != f["from_col"]]
                if (f["to_table"] in named_t or f["from_table"] in named_t) and meas:
                    measure = mcol if (mcol and mcol["table"] == f["from_table"]) else meas[0]
                    gcol = next((c for c in sch if c["table"] == f["to_table"] and c["name"] == f["to_col"]), None)
                    if gcol:
                        argmax = (measure, gcol)
                        if f["to_table"] in named_t:              # prefer the dim the user actually named
                            break
            # "the X who/with the most" = argmax (one row); "LIST/show X by highest" = a ranking (keep all rows)
            ranking = any(t in {"list", "show", "display", "all", "rank", "every", "each"} for t in low)
            lim = None if ranking else 1
            if argmax:
                measure, gcol = argmax                            # GROUP BY the dim key; SELECT a friendly name col if any
                disp = next((c for c in sch if c["table"] == gcol["table"] and c["affinity"] == "TEXT" and c["name"] != gcol["name"]), None)
                slots["agg"] = ("SUM", measure["table"], measure["name"])
                slots["group_by"] = ([gcol, disp] if disp else [gcol])   # GROUP BY the displayed col too (Postgres
                slots["order_by"] = [("__agg__", sdir)]; slots["limit"] = lim   # strict GROUP BY; SQLite-equivalent)
                slots["select"] = [disp] if disp else []
            elif mcol:                                            # single-table superlative: sort by the measure
                slots["order_by"] = [(mcol, sdir)]; slots["limit"] = lim

        # projected columns the question names explicitly — plural-insensitive ("show emails" -> Email) and
        # multi-word names match when ALL their words appear ("first names" -> First Name)
        named = []
        for c in sch:
            words = name_words(c["name"])
            if (c["name"].lower() in low
                    or (len(words) == 1 and any(wmatch(t, words[0]) for t in low))
                    or (len(words) > 1 and all(any(wmatch(t, w) for t in low) for w in words))):
                named.append(c)
        used = ({c["name"] for c, _ in slots["order_by"] if isinstance(c, dict)} | {c["name"] for c in slots["group_by"]}
                | {w[0]["name"] for w in slots["where"]})
        proj = any(t in {"show", "list", "display", "select", "get", "give"} for t in low)
        if slots["agg"]:
            slots["select"] = slots["select"] or list(slots["group_by"])   # argmax may already have set a display col
        elif proj:
            slots["select"] = named
        else:
            slots["select"] = [c for c in named if c["name"] not in used]
        if {"unique", "distinct"} & set(low) and not slots["agg"] and named:   # "unique countries" -> SELECT DISTINCT
            slots["distinct"] = True
            if not slots["select"]:
                slots["select"] = named[:1]

        # which tables are involved -> the JOIN (deterministic FK between them)
        involved = []
        order_cols = [a for a, _ in slots["order_by"] if isinstance(a, dict)]
        for c in (slots["select"] + slots["group_by"] + order_cols
                  + [w[0] for w in slots["where"]] + ([sch_col_of(slots["agg"], sch)] if slots["agg"] and slots["agg"][1] else [])):
            if c is not None and c["table"] not in involved:
                involved.append(c["table"])
        tnames = {t["name"].lower(): t["name"] for t in tables}   # a table NAMED in the question (e.g. "count
        for tk in low:                                            # ORDERS per city") joins even w/ no column ref
            for tl, tn in tnames.items():
                if (tk == tl or tk.rstrip("s") == tl.rstrip("s")) and tn not in involved:
                    involved.append(tn)
        if not involved:
            involved = [tables[0]["name"]]
        join = self._pick_join(involved, fks, tables)
        return slots, join, involved, [self._fires(final[qstart + i], "intent") for i in range(len(toks))]

    def _pick_join(self, involved, fks, tables):
        """If >=2 involved tables share a discovered FK, return (fact, dim, fk_col, pk_col). Else None."""
        names = set(involved) if len(involved) >= 2 else set()
        for f in fks:
            if f["from_table"] in names and f["to_table"] in names:
                return (f["from_table"], f["to_table"], f["from_col"], f["to_col"])
        if len(involved) >= 2:                                    # involved tables with no direct FK: use any FK touching one
            for f in fks:
                if f["from_table"] in involved or f["to_table"] in involved:
                    return (f["from_table"], f["to_table"], f["from_col"], f["to_col"])
        return None

    def ast_semantic_signals(
        self, question, sch, proposal_model=None, proposal_question_vector=None
    ):
        """Encode role-specific question phrases in the same metric space as schema columns."""
        if proposal_model is not None:
            from engine.sql_proposal_runtime import ProposalSignalProvider

            provider = getattr(self, "_ast_proposal_provider", None)
            if provider is None or provider.model is not proposal_model:
                provider = self._ast_proposal_provider = ProposalSignalProvider(
                    proposal_model, self
                )
            return provider.signals_from_descriptors(
                question, sch, proposal_question_vector
            )
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
                   use_semantic_signals=True, phase2=True, phase3=True, phase4=True,
                   phase5=True, rank_model=None, proposal_model=None,
                   proposal_question_vector=None, profile_config=None):
        """Return ranked, typed SQL AST candidates for the deterministic planner.

        This is parallel to ``plan``/``assemble`` during rollout: callers can compare both planners without
        changing the established serving route.  ``tables`` stays in the signature to match ``plan`` and make
        the boundary explicit; the rich ``sch`` already contains its values and inferred types.
        """
        from engine.sql_search import SQLSearcher, SchemaGraph
        graph = SchemaGraph.from_planner(sch, fks)
        signals = (
            self.ast_semantic_signals(
                question, sch, proposal_model, proposal_question_vector
            )
            if use_semantic_signals else None
        )
        candidates = SQLSearcher(
            graph, beam_size=beam_size, max_candidates=max_candidates
        ).search(
            question, semantic_signals=signals, phase2=phase2, phase3=phase3, phase4=phase4,
            phase5=phase5, profile_config=profile_config,
        )
        if rank_model is None:
            return candidates
        if proposal_model is not None:
            from engine.sql_learned_rank import rerank_with_promotion_gate

            return rerank_with_promotion_gate(rank_model, question, candidates)
        return rank_model.rerank(question, candidates)

    def _ast_models(self, mode):
        """Load explicitly requested frozen artifacts once per TableQuery instance."""
        cached = self._ast_runtime_models.get(mode)
        if cached is not None:
            return cached
        from engine.config import sql_proposer_path, sql_ranker_path

        proposer = ranker = None
        if mode in {"ast_profile", "ast_strict"}:
            from engine.sql_proposal import SQLProposalModel

            path = sql_proposer_path()
            if not path.exists():
                raise RuntimeError(f"SQL proposer artifact not found: {path}")
            proposer = SQLProposalModel.load(str(path))
        if mode == "ast_strict":
            from engine.sql_learned_rank import load_ranker_model

            path = sql_ranker_path()
            if not path.exists():
                raise RuntimeError(f"SQL ranker artifact not found: {path}")
            ranker = load_ranker_model(str(path))
            if "promotion_gate" not in ranker.metadata:
                raise RuntimeError("SQL strict ranker has no held-out promotion gate")
        self._ast_runtime_models[mode] = (proposer, ranker)
        return proposer, ranker

    def _serve_ast(self, question, norm, fks, sch, tablemap, mode):
        from engine.sql_profile_expansion import ProfileSearchConfig

        proposer, ranker = self._ast_models(mode)
        candidates = self.search_ast(
            question, sch, norm, fks,
            max_candidates=180 if proposer is not None else 25,
            use_semantic_signals=True,
            proposal_model=proposer,
            rank_model=ranker,
            profile_config=ProfileSearchConfig() if proposer is not None else None,
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

    # ---------- assembly ----------
    def assemble(self, slots, join, involved, sch):
        toks = []

        def push(text, kind="kw", table=None, col=None, raw=None, clause=None):
            toks.append({"text": text, "kind": kind, "table": table, "col": col, "raw": raw, "clause": clause})

        def field(c, clause):
            push(qident(c["table"]), "table", c["table"], None, c["table"], clause); push(".", "op", clause=clause)
            push(qident(c["name"]), "field", c["table"], c["idx"], c["name"], clause)

        push("SELECT", clause="proj")
        if slots.get("distinct"):
            push("DISTINCT", clause="proj")
        first = True
        for c in slots["select"]:                                 # the chosen projection: group cols for an agg,
            if not first:                                         # the display col for an argmax, else named cols
                push(",", "punc", clause="proj")
            field(c, "proj"); first = False
        if slots["agg"]:
            if not first:
                push(",", "punc", clause="proj")
            fn, at, ac = slots["agg"]
            push(fn + "(", clause="proj")
            if ac == "*":
                push("*", "punc", clause="proj")
            else:
                tcol = next((c for c in sch if c["table"] == at and c["name"] == ac), None)
                field(tcol, "proj") if tcol else push("*", "punc", clause="proj")
            push(")", clause="proj"); first = False
        if first:
            push("*", "punc", clause="proj")

        if join:
            fact, dim, fk, pk = join
            push("FROM", clause="source"); push(qident(fact), "table", fact, None, fact, "source")
            push("JOIN", clause="join"); push(qident(dim), "table", dim, None, dim, "join")
            push("ON", clause="join")
            push(qident(fact), "table", fact, None, fact, "join"); push(".", "op", clause="join")
            push(qident(fk), "field", fact, None, fk, "join"); push("=", "op", clause="join")
            push(qident(dim), "table", dim, None, dim, "join"); push(".", "op", clause="join")
            push(qident(pk), "field", dim, None, pk, "join")
        else:
            push("FROM", clause="source"); push(qident(involved[0]), "table", involved[0], None, involved[0], "source")

        if slots["where"]:
            push("WHERE", clause="filter")
            for i, w in enumerate(slots["where"]):
                c, op, val = w[0], w[1], w[2]
                wrap = w[3] if len(w) > 3 else None
                if i:
                    push("AND", clause="filter")
                if wrap == "year":                                # compare the ISO year prefix of a date column
                    push("substr(", "op", clause="filter"); field(c, "filter"); push(",1,4)", "op", clause="filter")
                    push(op, "op", clause="filter")
                    push(qlit(val), "literal", c["table"], c["idx"], val, "filter")
                    continue
                field(c, "filter"); push(op, "op", clause="filter")
                if (c["affinity"] in ("INTEGER", "REAL") or "is_num" in c["struct"]) and re.fullmatch(r"-?\d+(\.\d+)?", str(val)):
                    push(str(val), "literal", c["table"], c["idx"], val, "filter")
                else:
                    push(qlit(val), "literal", c["table"], c["idx"], val, "filter")
        if slots["group_by"]:
            push("GROUP", clause="group"); push("BY", clause="group")
            for i, c in enumerate(slots["group_by"]):
                if i:
                    push(",", "punc", clause="group")
                field(c, "group")
        if slots["order_by"]:
            push("ORDER", clause="order"); push("BY", clause="order")
            for i, (c, d) in enumerate(slots["order_by"]):
                if i:
                    push(",", "punc", clause="order")
                if c == "__agg__" and slots["agg"]:               # ORDER BY the aggregate itself (superlative / argmax)
                    fn, at, ac = slots["agg"]
                    push(fn + "(", clause="order")
                    if ac == "*":
                        push("*", "punc", clause="order")
                    else:
                        tcol = next((x for x in sch if x["table"] == at and x["name"] == ac), None)
                        field(tcol, "order") if tcol else push("*", "punc", clause="order")
                    push(")", clause="order")
                else:
                    field(c, "order")
                push(d, clause="order")
        if slots["limit"]:
            push("LIMIT", clause="limit"); push(str(slots["limit"]), "literal", clause="limit")
        return " ".join(t["text"] for t in toks), toks

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

    def inspect_layers(self, tables, fks, sch, toks):
        units, colidx = self._schema_name_units(tables, fks)
        base = len(units)
        for t in toks:
            text = t["raw"] if (t["kind"] in ("field", "literal", "table") and t.get("raw") is not None) else t["text"]
            col = colidx.get((t.get("table"), t.get("raw")), -1) if t["kind"] == "field" else -1
            units.append({"text": str(text), "group": "sql", "kind": t["kind"], "table": t.get("table"),
                          "col": col, "colname": t.get("raw") if t["kind"] == "field" else None,
                          "row": -3, "clause": t.get("clause")})
        x = self._encode([u["text"] for u in units])
        layers = self._layers(units, x)
        out = []
        for k, t in enumerate(toks):
            fin = layers[-1][base + k]
            srole = self._fires(fin, "srole"); clause = self._fires(fin, "clause"); place = self._fires(fin, "ace", 0.4)[:3]
            salient = ([("srole_" + s, s) for s in srole] + [("clause_" + c, c) for c in clause]
                       + [(d["name"], dim_label(d["name"])[1]) for d in self.dims
                          if d["family"] == "ace" and dim_label(d["name"])[1] in place])
            evo = [{lb: round(float(min(1.0, max(0.0, layers[L][base + k][self.sid[dn]]))), 3) for dn, lb in salient}
                   for L in range(self.nL)]
            out.append({"text": t["text"], "kind": t["kind"], "srole": srole, "clause": clause,
                        "place": place, "evolution": evo})
        return out

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

    def serve(self, tables, question):
        """tables: [{name, columns, rows}]. Full multi-table pipeline for the web UI."""
        norm, fks = self.ingest(tables)
        sch, colidx, tablemap = self.schema(norm, fks)
        from engine.config import sql_planner_mode

        mode = sql_planner_mode()
        if mode != "legacy":
            try:
                candidate, result, err, candidates = self._serve_ast(
                    question, norm, fks, sch, tablemap, mode
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
                "tables": [{
                    "name": table["name"], "columns": table["columns"],
                    "n_rows": len(table["rows"]), "dropped": table.get("_dedup_dropped", 0),
                } for table in norm],
                "fks": [{
                    "from": qual(fk["from_table"], fk["from_col"]),
                    "to": qual(fk["to_table"], fk["to_col"]), "conf": fk["conf"],
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
                "model": f"engine - deterministic typed SQL AST planner ({mode})",
            }
        slots, join, involved, _ = self.plan(question, sch, norm, fks)
        sql, toks = self.assemble(slots, join, involved, sch)
        ok, why = self.guard(sql)
        result, err = None, None
        if ok:
            try:
                cols, rws = self.execute(tablemap, sch, sql)
                result = {"columns": cols, "rows": [["" if v is None else v for v in r] for r in rws[:50]]}
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        else:
            err = "guard: " + why
        return {"question": question, "sql": sql, "valid": ok, "error": err, "result": result,
                "tables": [{"name": t["name"], "columns": t["columns"], "n_rows": len(t["rows"]),
                            "dropped": t.get("_dedup_dropped", 0)} for t in norm],
                "fks": [{"from": qual(f["from_table"], f["from_col"]), "to": qual(f["to_table"], f["to_col"]),
                         "conf": f["conf"]} for f in fks],
                "join": ({"fact": join[0], "dim": join[1], "on": f'{qual(join[0], join[2])} = {qual(join[1], join[3])}'}
                         if join else None),
                "schema": [{"table": c["table"], "name": c["name"], "affinity": c["affinity"], "ace": c["ace"]} for c in sch],
                "tokens": self.inspect_layers(norm, fks, sch, toks), "n_layers": self.nL,
                "model": "engine - Qwen encoder + relational model; deterministic FK + named-dim slot-filling -> multi-table SQL"}


def sch_col_of(agg, sch):
    if not agg or agg[1] is None:
        return None
    return next((c for c in sch if c["table"] == agg[1] and c["name"] == agg[2]), None)
