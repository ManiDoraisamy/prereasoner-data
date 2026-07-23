"""The UNIFIED-ENCODER overlay. Identical to TableQuery (the anchored readout + analyze/planner) EXCEPT the
encoder is the contrastively-fine-tuned Qwen (base + LoRA adapter from data/qwen_lora) and the relational
readout is the trained RelationalModel (encoder.pt). So the SAME named-dim readout (taxonomy / datatype /
intent) runs on an encoder that is ALSO a metric space — one model for resolution-fuzzy + the anchored
readout. Entity resolution itself stays on bge + altLabel-exact (engine.entities); this encoder powers the
readout, the operator/intent decision, and the free-text bridge embeddings.
"""
from __future__ import annotations

import numpy as np
import torch

from engine.artifact_provenance import sha256_tree, validate_weight_bundle
from engine.config import DATA_DIR
from engine.tables import TableQuery, MODEL_ID
from engine.encoder_model import RelationalModel


def load_encoder(obj, deploy_dir=DATA_DIR):
    """ONE MODEL: the trained unified encoder IS the world encoder — operator (intent_agg dims), bridge
    embeddings, AND column typing all read off it. Sets the shared attributes (alloc/nc/dims/sid/thr/model/
    nL/tok/qwen/hdim) from the shipped artifacts (encoder_meta.pt / encoder.pt / qwen_lora) + the anchor-head
    thresholds (the intent dims fire the operator; verified SUM/COUNT/AVG)."""
    from pathlib import Path
    d = Path(deploy_dir)
    obj.model_bundle_sha256 = validate_weight_bundle(d)
    obj.encoder_adapter_sha256 = sha256_tree(d / "qwen_lora")
    obj.encoder_data_dir = str(d.resolve())
    pt = torch.load(d / "encoder_meta.pt", map_location="cpu", weights_only=False)
    obj.alloc = pt["alloc"]; obj.nc = obj.alloc["n_content"]
    obj.dims = sorted(obj.alloc["dims"], key=lambda x: x["dim_id"])
    obj.sid = {dm["name"]: dm["dim_id"] for dm in obj.dims}
    z = np.load(d / "anchor_assignment.npz", allow_pickle=True)           # per-dim Youden-J thresholds (incl. intent)
    obj.thr = {str(n): float(t) for n, t in zip(z["dims"], z["thr"])}
    # OPERATOR gate: anchor_assignment's intent thresholds (~0.017) were calibrated on per-CELL tokens, so they
    # let NON-aggregate QUESTIONS clear the gate (false is_agg -> hybrid/clarify hijack). On questions, real
    # SUM/AVG fire 0.8+, real COUNT ~0.07, and non-aggregates <0.08 — so gate SUM/AVG at 0.30 and COUNT at 0.05
    # (COUNT's signal is weak, like the city dim). Read off THIS model; separates total/how-many/average from
    # 'who complained…'/'list customers'/'customers in France'.
    obj.thr.update({"intent_agg_sum": 0.30, "intent_agg_avg": 0.30, "intent_agg_count": 0.05})
    obj.model = RelationalModel(**pt["cfg"])
    obj.model.load_state_dict(torch.load(d / "encoder.pt", map_location="cpu")); obj.model.eval()
    obj.nL = pt["cfg"]["layers"] + 1
    from transformers import AutoModel, AutoTokenizer
    from peft import PeftModel
    obj.tok = AutoTokenizer.from_pretrained(MODEL_ID)
    if obj.tok.pad_token is None:
        obj.tok.pad_token = obj.tok.eos_token
    base = AutoModel.from_pretrained(MODEL_ID, low_cpu_mem_usage=True).float()
    obj.qwen = PeftModel.from_pretrained(base, str(d / "qwen_lora")).eval()
    obj.hdim = base.config.hidden_size


class EncoderQuery(TableQuery):
    """TableQuery with the unified (LoRA-fine-tuned) Qwen encoder + the trained relational readout loaded."""

    def __init__(self, deploy_dir=DATA_DIR):
        super().__init__(deploy_dir)
        load_encoder(self, deploy_dir)

    # ---------- operator FROM THE MODEL (retires the keyword AGG_CUES) ----------
    INTENT_OPS = {"COUNT": "intent_agg_count", "SUM": "intent_agg_sum", "AVG": "intent_agg_avg"}

    def _question_readout(self, tables, fks, question):
        """Build the (schema-name units + question-token units) graph, run the unified encoder + readout, and
        return (final_layer_readout, qstart, toks, low). The intent dims live on the question tokens at
        final[qstart + i]."""
        toks = question.split(); low = [t.lower() for t in toks]
        units, _ = self._schema_name_units(tables, fks)
        qstart = len(units)
        units += [{"text": t, "group": "q", "kind": "q", "table": None, "col": -1, "colname": None, "row": -2}
                  for t in toks]
        x = self._encode([u["text"] for u in units])
        final = self._layers(units, x)[-1]
        return final, qstart, toks, low

    def read_op_model(self, tables, question, fks=None):
        """OPERATOR FROM THE MODEL, not keywords. The unified encoder fires intent_agg_sum on 'sell'/'how much',
        intent_agg_count on 'how many', intent_agg_avg on 'average' — even when NO lexical cue from AGG_CUES is
        present ('how much did we sell' has no 'sum'/'total' token). Returns (op|None, {op: score}). Operand tokens
        (column names + cell values) are excluded so the intent reads off the QUESTION verb, not the data."""
        tables, fks = (tables, fks) if fks is not None else self.ingest(tables)
        final, qstart, toks, low = self._question_readout(tables, fks, question)
        operand = set()
        for t in tables:
            for c in t["columns"]:
                operand.add(str(c).lower()); operand.update(str(c).lower().split())
            for r in t["rows"]:
                for v in r:
                    if v is not None:
                        operand.add(str(v).lower())
        cand = [i for i in range(len(toks)) if low[i] not in operand] or list(range(len(toks)))

        def score(name):
            dd = self.sid[name]
            return max((float(final[qstart + i][dd]) for i in cand), default=0.0)

        scores = {op: score(dim) for op, dim in self.INTENT_OPS.items()}
        op, sc = max(scores.items(), key=lambda kv: kv[1])
        thr = self.thr.get(self.INTENT_OPS[op], 0.5)
        runner = max((v for k, v in scores.items() if k != op), default=0.0)
        # accept on the calibrated Youden-J threshold OR on clear dominance: the intent is plainly present
        # (>=0.5) AND unambiguous (margin over the runner-up op >=0.4). The dominance arm recovers high-
        # confidence aggregates that land a hair under a tight cut (e.g. "how many orders" at 0.873 vs a 0.874
        # COUNT threshold) while admitting NO non-aggregates — their argmax intent stays <0.5 (max observed 0.32).
        accept = sc >= thr or (sc >= 0.5 and (sc - runner) >= 0.4)
        return (op if accept else None), scores

    def _salient_evo(self, layers, ui):
        """Override: use PER-DIM calibrated thresholds (self.thr) instead of the hardcoded 0.4/0.5. The
        unified encoder's sparse entity dims fire at much lower magnitudes (calibrated to 0.03-0.06 by Youden's
        J); the parent's 0.4 cut would miss all of them. Every dim that fires above its calibrated threshold is
        included in the evolution payload so the client can display it."""
        fams = {"struct", "nsm_cat", "nsm_prime", "ace"}
        ddims = [d for d in self.dims if d["family"] in fams]
        fin = layers[-1][ui]
        fired = [d["name"] for d in ddims if fin[d["dim_id"]] >= self.thr.get(d["name"], 0.5)]
        amax = max(ddims, key=lambda d: fin[d["dim_id"]])["name"]
        salient = sorted(set(fired) | {amax})
        return [{nm: round(float(min(1.0, max(0.0, layers[L][ui][self.sid[nm]]))), 3) for nm in salient}
                for L in range(self.nL)]

    @staticmethod
    def _is_id(name):
        """Structural surrogate-key exclusion (a primary/foreign key is never a SUM/AVG measure). This is the
        ONLY hardcoded rule left in operand selection — it is structural plumbing, not a measure-noun lookup."""
        import re as _re
        return bool(_re.search(r"(^id$|_?id$|^index$|^pk$)", name.lower()))

    def read_op_all(self, question, sch):
        """Operator + operand FROM THE UNIFIED METRIC SPACE — NO `MEASURE_NOUNS` / `table_noun` keyword lists.
        sch: list of {table, name, affinity[, qvec]} (planner format; qvec present on the live path).
          - op (SUM/COUNT/AVG/None) comes from read_op_model (the question's intent_agg dims).
          - the COUNT table and the SUM/AVG measure column are chosen by COSINE in the contrastive space
            (the reason the encoder is unified: 'sell'/'earn'/'revenue'/'amount' land together), restricted to
            non-id numeric columns. An explicitly-named column/table wins; a single measure is taken directly.
        Returns (fn, table, col) | ("COUNT", table|None, None) | None, the format KnowledgeTableQuery.serve() expects."""
        import numpy as _np
        # rebuild tables from sch (incl. per-column `values` when the rich planner sch carries them) so ingest()'s
        # inclusion-dependency FK discovery runs — the fk edges shift the intent readout (the high COUNT threshold
        # is sensitive to them). read_op_model only encodes schema-NAME + question units, so reconstructed rows are
        # cheap (they affect relate(), not the encode). Lightweight sch (no values) degrades to empty rows.
        by_table = {}
        for c in sch:
            e = by_table.setdefault(c["table"], {"cols": [], "vals": []})
            e["cols"].append(c["name"]); e["vals"].append(list(c.get("values") or []))
        stub_tables = []
        for tname, e in by_table.items():
            nrow = min(max((len(v) for v in e["vals"]), default=0), 24)
            rows = [[(e["vals"][ci][ri] if ri < len(e["vals"][ci]) else None) for ci in range(len(e["cols"]))]
                    for ri in range(nrow)]
            stub_tables.append({"name": tname, "columns": e["cols"], "rows": rows})
        if not stub_tables:
            return None
        norm, fks = self.ingest(stub_tables)
        op, _ = self.read_op_model(norm, question, fks)
        if op is None:
            return None
        tnames = sorted(by_table)
        low = question.lower().split()
        nonid_num = [c for c in sch if c.get("affinity") in ("INTEGER", "REAL") and not self._is_id(c["name"])]

        # encode the question + table names + any column names lacking a cached qvec, in ONE batch
        miss = [c for c in nonid_num if c.get("qvec") is None]
        texts = [question] + tnames + [c["name"] for c in miss]
        V = self._encode(texts)
        qv = V[0]
        tvec = {t: V[1 + i] for i, t in enumerate(tnames)}
        mv = {(c["table"], c["name"]): V[1 + len(tnames) + j] for j, c in enumerate(miss)}

        def cvec(c):
            return _np.asarray(c["qvec"], _np.float32) if c.get("qvec") is not None else mv[(c["table"], c["name"])]

        def cos(a, b):
            return float(a @ b / ((_np.linalg.norm(a) * _np.linalg.norm(b)) + 1e-9))

        def token_table():
            return next((t for w in low for t in tnames
                         if w == t or w == t + "s" or w.rstrip("s") == t.rstrip("s")), None)

        def token_measure():
            return next((c for c in nonid_num for w in low
                         if w == c["name"].lower() or w.rstrip("s") == c["name"].lower().rstrip("s")), None)

        if op == "COUNT":
            t = token_table() or (max(tnames, key=lambda t: cos(qv, tvec[t])) if tnames else None)
            return ("COUNT", t, None)
        # SUM / AVG: explicit measure token > (table-noun w/ no measure -> COUNT) > single measure > cosine measure
        tm = token_measure()
        if tm:
            return (op, tm["table"], tm["name"])
        tt = token_table()                                   # "total CUSTOMERS …" names the ENTITY, not a measure col ->
        if tt:                                               # COUNT that sheet's rows (matches KnowledgeTableQuery.read_op_all);
            return ("COUNT", tt, None)                       # an FK-reachable measure (orders.amount) must NOT hijack -> SUM
        if not nonid_num:                                    # nothing to sum -> it's a row count ("total customers")
            t = token_table() or (max(tnames, key=lambda t: cos(qv, tvec[t])) if tnames else None)
            return ("COUNT", t, None) if t else None
        if len(nonid_num) == 1:
            return (op, nonid_num[0]["table"], nonid_num[0]["name"])
        best = max(nonid_num, key=lambda c: cos(qv, cvec(c)))
        return (op, best["table"], best["name"])

    def answer(self, table, question):
        """End-to-end LOCAL demonstration of the OPERATOR FROM THE MODEL: read_op_model -> agg SQL -> SQLite exec.
        Single table, aggregate only (the world filter / multi-table joins are the /world serve path). Proves the
        headline 'how much did we sell' -> SUM(measure) without a keyword cue. Returns {op, sql, result, scores}."""
        import re as _re, sqlite3
        norm, fks = self.ingest([table])
        t = norm[0]; cols = t["columns"]; rows = t["rows"]
        op, scores = self.read_op_model(norm, question, fks)

        def _isnum(v):
            try:
                float(str(v).replace(",", "").lstrip("$").rstrip("%")); return True
            except (ValueError, TypeError):
                return False

        numcols = []
        for ci, c in enumerate(cols):
            if _re.search(r"(^id$|_id$|^index$)", str(c).lower()):
                continue
            nn = [r[ci] for r in rows if r[ci] is not None and str(r[ci]).strip()]
            if nn and sum(_isnum(v) for v in nn) >= 0.8 * len(nn):
                numcols.append(c)
        tn = t["name"]
        if op == "COUNT":
            sql = f'SELECT COUNT(*) FROM "{tn}"'
        elif op in ("SUM", "AVG") and numcols:
            sql = f'SELECT {op}("{numcols[0]}") FROM "{tn}"'
        else:
            op = op if op in ("SUM", "AVG", "COUNT") else None
            sql = f'SELECT * FROM "{tn}"'
        con = sqlite3.connect(":memory:")
        con.execute(f'CREATE TABLE "{tn}" ({", ".join(chr(34) + str(c) + chr(34) for c in cols)})')
        con.executemany(f'INSERT INTO "{tn}" VALUES ({", ".join("?" * len(cols))})', [list(r) for r in rows])
        result = con.execute(sql).fetchall()
        con.close()
        return {"op": op, "sql": sql, "result": result, "scores": scores}
