"""The UNIFIED-ENCODER overlay. Identical to TableQuery (the anchored readout + analyze/planner) EXCEPT the
encoder is the contrastively-fine-tuned Qwen (base + LoRA adapter from data/qwen_lora) and the relational
readout is the trained RelationalModel (encoder.pt). So the SAME named-dim readout (taxonomy / datatype /
intent) runs on an encoder that is ALSO a metric space — one model for resolution-fuzzy + the anchored
readout. Entity resolution itself stays on bge + altLabel-exact (engine.entities); this encoder powers the
readout, the operator/intent decision, and the free-text bridge embeddings.
"""
from __future__ import annotations

import re
from typing import ClassVar

import numpy as np

from engine.artifact_provenance import validate_weight_bundle
from engine.config import BASE_MODEL_REVISION as MODEL_REVISION
from engine.config import DATA_DIR
from engine.sql_schema import is_surrogate_key
from engine.tables import MODEL_ID, TableQuery


def load_encoder(obj, deploy_dir=DATA_DIR):
    """ONE MODEL: the trained unified encoder IS the world encoder — operator (intent_agg dims), bridge
    embeddings, AND column typing all read off it. Sets the shared attributes (alloc/nc/dims/sid/thr/model/
    nL/tok/qwen/hdim) from the shipped artifacts (encoder_meta.pt / encoder.pt / qwen_lora) + the anchor-head
    thresholds (the intent dims fire the operator; verified SUM/COUNT/AVG)."""
    from pathlib import Path

    import torch

    from engine.encoder_model import RelationalModel
    d = Path(deploy_dir)
    obj.model_bundle_sha256 = validate_weight_bundle(d)
    obj.encoder_data_dir = str(d.resolve())
    pt = torch.load(d / "encoder_meta.pt", map_location="cpu", weights_only=True)
    obj.alloc = pt["alloc"]; obj.nc = obj.alloc["n_content"]
    obj.dims = sorted(obj.alloc["dims"], key=lambda x: x["dim_id"])
    obj.sid = {dm["name"]: dm["dim_id"] for dm in obj.dims}
    z = np.load(d / "anchor_assignment.npz", allow_pickle=False)          # per-dim Youden-J thresholds (incl. intent)
    obj.thr = {str(n): float(t) for n, t in zip(z["dims"], z["thr"])}
    # Operator gates are calibrated on question graphs for one checkpoint. The promoted bundle's metadata
    # does not carry them (encoder_meta.pt holds alloc and cfg only), so these are its gates; a bundle that
    # records intent_thresholds replaces them.
    intent_thresholds = pt.get("intent_thresholds") or {
        "COUNT": 0.05,
        "SUM": 0.30,
        "AVG": 0.30,
    }
    required_ops = {"COUNT", "SUM", "AVG"}
    if set(intent_thresholds) != required_ops:
        raise ValueError("encoder metadata has invalid intent_thresholds")
    obj.thr.update({
        f"intent_agg_{op.lower()}": float(intent_thresholds[op])
        for op in sorted(required_ops)
    })
    obj.model = RelationalModel(**pt["cfg"])
    obj.model.load_state_dict(torch.load(
        d / "encoder.pt", map_location="cpu", weights_only=True
    )); obj.model.eval()
    obj.nL = pt["cfg"]["layers"] + 1
    from peft import PeftModel
    from transformers import AutoModel, AutoTokenizer
    obj.tok = AutoTokenizer.from_pretrained(MODEL_ID, revision=MODEL_REVISION)
    if obj.tok.pad_token is None:
        obj.tok.pad_token = obj.tok.eos_token
    base = AutoModel.from_pretrained(
        MODEL_ID, revision=MODEL_REVISION, low_cpu_mem_usage=True
    ).float()
    obj.qwen = PeftModel.from_pretrained(base, str(d / "qwen_lora")).eval()
    obj.hdim = base.config.hidden_size


def attach_question_rewriter(obj):
    """Give the planner selection's labelled Gemini fallback (engine/question_rewrite.py). Nothing is
    loaded: it calls Gemini only when the operator enabled it and the search found no runnable query."""
    from engine.question_rewrite import QuestionRewriter
    obj.question_rewriter = QuestionRewriter()


class EncoderQuery(TableQuery):
    """TableQuery with the complete runtime bundle loaded: the unified (LoRA-fine-tuned) Qwen encoder and
    the trained relational readout, whose named dimensions drive the deterministic SQL search."""

    def __init__(self, deploy_dir=DATA_DIR):
        super().__init__(deploy_dir)
        load_encoder(self, deploy_dir)
        attach_question_rewriter(self)

    # ---------- operator FROM THE MODEL (retires the keyword AGG_CUES) ----------
    INTENT_OPS: ClassVar[dict[str, str]] = {
        "COUNT": "intent_agg_count", "SUM": "intent_agg_sum", "AVG": "intent_agg_avg",
    }

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
        (column names + cell values) are excluded so the intent reads off the QUESTION verb, not the data.
        Closed-class tokens are excluded too: an aggregate is never expressed by 'a', 'which' or 'in'. The COUNT
        threshold is low (0.05), and the article in 'who ordered a trench coat in France' read 0.15, so a
        listing question was answered with a count (2026-09-27). 'many'/'much' are adjectives and still read."""
        from engine.closed_class import closed_class_words

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
        closed = closed_class_words(question)
        cand = [i for i in range(len(toks))
                if low[i] not in operand and low[i].strip(".,;:!?'\"()") not in closed] or list(range(len(toks)))

        def score(name):
            dd = self.sid[name]
            return max((float(final[qstart + i][dd]) for i in cand), default=0.0)

        scores = {op: score(dim) for op, dim in self.INTENT_OPS.items()}
        op, sc = max(scores.items(), key=lambda kv: kv[1])
        thr = self.thr.get(self.INTENT_OPS[op], 0.5)
        return (op if sc >= thr else None), scores

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

    def read_op_all(self, question, sch):
        """Operator + operand FROM THE UNIFIED METRIC SPACE — no local measure-noun lists (the one money-noun rule
        is shared with the own-data search in engine/sql_expansion.py).
        sch: list of {table, name, affinity[, qvec]} (planner format; qvec present on the live path).
          - op (SUM/COUNT/AVG/None) comes from read_op_model (the question's intent_agg dims).
          - the COUNT table and the SUM/AVG measure column are chosen by COSINE in the contrastive space
            (the reason the encoder is unified: 'sell'/'earn'/'revenue'/'amount' land together), restricted to
            non-id numeric columns. An explicitly-named column/table wins; a single measure is taken directly.
          - a money noun that names the table ("sales") reads as its money-named measure column unless the question
            counts or lists it — the same rule as the own-data search (sql_expansion.money_total_position).
          - a quantity superlative ranks totals of the column it names ("which country has the most deposits",
            sql_expansion.ranked_measure_columns) or counts of the rows it names ("the most banks",
            sql_expansion.ranked_row_tables), and a count of a noun a numeric column already counts is that
            column's total ("how many transfers", sql_expansion.counted_measure_columns).
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
        tnames = sorted(by_table)
        low = question.lower().split()
        nonid_num = [c for c in sch if c.get("affinity") in ("INTEGER", "REAL") and not is_surrogate_key(c["name"])]
        from engine.sql_expansion import (
            counted_measure_columns, money_total_columns, ranked_measure_columns, ranked_row_tables,
        )
        money = money_total_columns(question, sch)
        # "which country has the most deposits" reads no aggregate in the model and "the fewest deposits" reads
        # a count; a superlative of quantity over a measure column asks for the top total (Chrome exploration,
        # 2026-10-01).
        ranked = ranked_measure_columns(question, sch) if op in (None, "COUNT") else []
        # "which country has the most banks" counts the rows of the sheet that lists them.
        rows = ranked_row_tables(question, sch) if op is None and not ranked and not money else []
        if op is None and not money and not ranked and len(rows) != 1:
            return None

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

        def name_words(column):
            return [word.rstrip("s") for word in re.findall(r"[a-z]+", column["name"].lower())]

        def token_measure():
            exact = next((c for c in nonid_num for w in low
                          if w == c["name"].lower() or w.rstrip("s") == c["name"].lower().rstrip("s")), None)
            if exact:
                return exact
            # A column named in more than one word ('weight kg') is named when every word of it is in the
            # question, or by its first word when that word starts no other numeric column ('total weight
            # for deliveries'). Missing it let the table noun "deliveries" read as a row count: "total weight
            # kg for deliveries in Germany" answered COUNT(*) = 1 instead of 2 (2026-09-30).
            said = {w.strip("?,.!").rstrip("s") for w in low}
            heads = [name_words(c)[:1] for c in nonid_num]
            named = [c for c in nonid_num
                     if (words := name_words(c))
                     and (set(words) <= said
                          or (words[0] in said and len(words[0]) > 2 and heads.count([words[0]]) == 1))]
            return named[0] if len(named) == 1 else None

        def closest(columns):
            return columns[0] if len(columns) == 1 else max(columns, key=lambda c: cos(qv, cvec(c)))

        def money_total():
            if not money:
                return None
            table, columns = money
            return table, closest(columns)["name"]

        if ranked:                                           # "which bank has the most deposits"
            column = closest(ranked)
            return ("SUM", column["table"], column["name"])
        if op is None:
            if money:                                        # "what's the sales in London": the head read no
                return ("SUM", *money_total())               # aggregate, but the money noun asks for the total
            return ("COUNT", rows[0], None)
        if op == "COUNT":
            total = money_total()                            # "whats the sales in france": no count was asked
            if total:
                return ("SUM", *total)
            counted = counted_measure_columns(question, sch)
            if counted:                                      # "how many transfers in Canada" (2026-10-02): the
                column = closest(counted)                    # transfers column holds each hospital's count
                return ("SUM", column["table"], column["name"])
            t = token_table() or (max(tnames, key=lambda t: cos(qv, tvec[t])) if tnames else None)
            return ("COUNT", t, None)
        # SUM / AVG: explicit measure token > money noun naming the table > (table-noun w/ no measure -> COUNT)
        # > single measure > cosine measure
        tm = token_measure()
        if tm:
            return (op, tm["table"], tm["name"])
        total = money_total()
        if total:
            return (op, *total)
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
        import sqlite3
        norm, fks = self.ingest([table])
        t = norm[0]; cols = t["columns"]; rows = t["rows"]
        op, scores = self.read_op_model(norm, question, fks)

        def _isnum(v):
            try:
                from engine.numeric import parse_decimal
                parse_decimal(v)
                return True
            except (ValueError, TypeError):
                return False

        numcols = []
        for ci, c in enumerate(cols):
            if is_surrogate_key(str(c)):
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
