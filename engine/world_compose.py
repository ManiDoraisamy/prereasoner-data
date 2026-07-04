"""ComposedWorldQuery: the view-stacking COMPOSITION reasoner wired onto the live world path.

A world question with composition DEPTH (year-over-year, top-N, share, cumulative, ratio, sort) or more than
one uploaded table is answered by a STACK of views — the analytical primitives composed by depth
(engine.compose) over a BASE relation that is the uploaded FK join + the world-meaning join. A plain
aggregate / hybrid-semantic / clarify query DELEGATES to WorldQuery unchanged. So composition ADDS depth
without regressing anything.

Reuse, not reinvention:
  * the WORLD lookup (city -> country) comes from WorldQuery's world schema (world."words"); the uploaded data
    is already in the request, so the only thing Postgres provides for the composed path is the meaning lookup.
  * the analytical DAG is the deterministic ComposeEngine (filter/time/group/yoy/running/share/divide/having/
    top-N/sort), driven by the LEARNED 10-primitive head on the SAME unified encoder.
  * operator + measure come from the metric space (read_op_model + cosine), like the rest of the program.

Auth (the verified Google sub) is enforced by the server; the composed path reads only the shared read-only
world schema + the request's own data (no per-user schema read -> no IDOR).
"""
from __future__ import annotations
import re

from engine.tables import qident
from engine.entities import WORLD_TABLE_TYPE
from engine.world_query import WorldQuery
from engine.primitive_head import PrimitiveReader
from engine.compose import ComposeEngine


class ComposedWorldQuery:
    """Composition over the live multi-table + world base; delegates non-composed queries to WorldQuery."""

    # The composition primitives this layer ADDS over the delegate (WorldQuery already does plain aggregate +
    # FK/world joins + list-vs-aggregate). A row COUNT is read separately off read_op_model (it is an operator, not
    # a head dim) because the delegate's count+world path is weak.
    DEPTH_PRIMS = frozenset({"EXCL", "RATIO", "TOPN", "SHARE", "TIME", "HAVING", "SORT", "DIVIDE", "RUNNING"})

    def __init__(self):
        self.qw = WorldQuery()                            # resolution + world DB + auth + bridge machinery
        self.reader = PrimitiveReader(encoder=self.qw)    # the learned 10-primitive head on the SAME unified encoder
        self.reason = ComposeEngine(reader=self.reader)   # the deterministic composition engine

    # a distinctive WORLD numeric attribute (a measure the delegate can't expose as a column) -> needs the world join
    # + analytical stack even for a plain aggregate ("average population of cities"). Narrow + unambiguous on purpose.
    # (Lexical for now; making "which world attribute the question needs" a learned readout is a next step.)
    WORLD_MEASURES = re.compile(r'\bpopulation\b|atomic\s+(?:number|mass)', re.I)

    def _composed(self, tables, question):
        """LEARNED compose gate (no lexical DEPTH regex): route to the engine when the unified encoder READS a
        composition primitive (the head) or a COUNT intent (read_op_model), OR the question names a distinctive world
        MEASURE the delegate can't expose (population / atomic number). Plain SUM/AVG, bare group-by, list, and
        hybrid-semantic queries (no such readout) delegate to WorldQuery — which itself distinguishes list from
        aggregate. So whether a question needs composition is mostly a MODEL readout, not a keyword list."""
        if self.WORLD_MEASURES.search(question or ""):
            return True
        try:
            # A bare COUNT no longer gates to the engine. The delegate's count+world path was strengthened (the
            # entity-count fix: "total/how many <entity> [in <place>]" -> COUNT via the qid world join), and a
            # spurious COUNT readout on a PROJECTION ("which continent is Kyoto in") would otherwise hijack it into
            # the engine's count. Counts WITH composition depth still gate via the DEPTH_PRIMS arm (e.g. top-N by
            # count); a plain count delegates to the fixed WorldQuery, and serve() still re-expresses a clean
            # world-filtered scalar as the view stack (guarded by _same_answer) so the reasoning is still shown.
            return bool(self.reader.present(question) & self.DEPTH_PRIMS)
        except Exception as e:                            # noqa: BLE001 — the gate must never break the world path
            print("compose gate failed, delegating:", e, flush=True)
            return False

    # The analytical attributes of each resolved-entity TYPE: (source table, key column, [(world_col, exposed_name)]).
    # The bridge gives world_type + world_key (qid for a city, canonical name otherwise); we join the source table by
    # that key to expose the FULL resolved-entity row (population / atomic_number / mass / ...), not just country.
    # Entities that have a country are additionally chained country -> continent + currency (world."Countries").
    ENTITY_ATTRS = {
        "city":    ("Cities",    "qid",  [("country", "country"), ("population", "population")]),
        "state":   ("States",    "name", [("country", "country"), ("population", "population"), ("level", "level")]),
        "country": ("Countries", "name", [("continent", "continent"), ("currency_name", "currency")]),
        "element": ("Elements",  "name", [("symbol", "symbol"), ("atomic_number", "atomic_number"), ("mass", "mass")]),
        "place":   ("Places",    "name", [("kind", "kind"), ("hemisphere", "hemisphere"), ("population", "population")]),
    }

    def _world_lookup(self, norm, sub):
        """An in-memory world table whose first column is the upload's geo value and whose remaining columns are the
        FULL resolved-entity row (same resolution the delegate path uses: route() finds the geo column, the connected-
        bridge build does keyed disambiguation). The deterministic engine joins on the upload's own value; TEXT
        attributes (country/continent/currency/level/...) are filter dimensions, NUMERIC ones (population/atomic_
        number/mass) are measures the primitives can aggregate."""
        self.qw._pg_schema = sub
        cur = self.qw._rconn().cursor()
        _ridx = [0]                                       # 'Resolving <col>' slide index, across all string columns
        result = None                                     # the engine's world table = the FIRST table with connected entities
        for t in norm:
            ent, geocol = {}, None                        # value -> {attr: v, "_wt": type, "_wk": key}
            routed = {c: f for (tn, c), f in self.qw.route(t).items() if tn == t["name"]}
            for ci, col in enumerate(t["columns"]):       # column ORDER, so the resolve slides match the table layout
                cells = [str(rw[ci]) for rw in t["rows"] if ci < len(rw) and rw[ci] not in (None, "")]
                if not cells:
                    continue
                wtype = WORLD_TABLE_TYPE.get(routed.get(col)) if routed.get(col) else None
                if wtype:                                 # CONNECTED string column -> resolve + the wikipedia slide
                    try:
                        if wtype == "city":
                            self.qw._city_bridge_sql(norm, t["name"], col, None)
                        else:
                            self.qw._cell_bridge_sql(norm, t["name"], col, wtype, None)
                        bn = self.qw._conn_bridge_name(t["name"])
                        cur.execute(f'SELECT "value", "world_type", "world_key", "country" FROM {qident(sub)}.{qident(bn)} '
                                    f'WHERE "column" = %s', (col,))
                        col_rows = cur.fetchall()
                        for val, wt, wk, country in col_rows:
                            d = ent.setdefault(val, {})   # first resolution per value wins
                            d.setdefault("_wt", wt); d.setdefault("_wk", wk)
                            if country and "country" not in d:
                                d["country"] = country
                        geocol = col
                        # Stream ONE 'Resolving <col>' slide (this column's wikipedia."<type>" rows) LIVE, ahead of the
                        # view stack. Reads the persisted bridge, so it fires fresh OR cached; uses the server's emit
                        # CONTEXT (set under its LOCK), so _world_lookup needn't thread `emit` through.
                        self._emit_resolve_slide(cur, t["name"], col, col_rows, _ridx)
                    except Exception as e:                # noqa: BLE001 — a column that won't resolve is skipped
                        print("world lookup skipped", t["name"], col, ":", e, flush=True)
                elif not self._is_numeric(cells):         # UNCONNECTED free-text string column -> the embedding slide
                    self._emit_unconnected_slide(t["name"], col, _ridx)
            if ent and result is None:
                self._enrich(cur, ent)                    # join each entity's source table + country -> continent/currency
                attrs = sorted({k for d in ent.values() for k in d if not k.startswith("_")})
                result = {"name": "world meaning", "columns": [geocol] + attrs,
                          "rows": [[v] + [d.get(a) for a in attrs] for v, d in ent.items()]}
        return result

    def _emit_resolve_slide(self, cur, table, column, col_rows, ridx):
        """Stream ONE resolution slide for a CONNECTED string column: the wikipedia."<type>" rows (qid + the faithful
        Wikidata columns it lazy-filled) for the entities this column resolved to. The client renders <table> with
        <column> highlighted above this world table, whose `name` column is highlighted to match. Best-effort."""
        try:
            from engine.trace import ctx_emit
            wt = next((r[1] for r in col_rows if r[1]), None)               # the world_type (e.g. 'city')
            qids = sorted({r[2] for r in col_rows if r[2]})                 # the resolved world keys (qids)
            if not wt or not qids:
                return
            wtable = wt                                                     # wikipedia table = the EXACT Wikidata label
            tq = getattr(self.qw, "TYPE_QID", {}).get(wt)
            if tq:
                cur.execute('SELECT label FROM world."types" WHERE qid=%s', (tq,))
                _r = cur.fetchone()
                if _r and _r[0]:
                    wtable = str(_r[0])[:63]
            cur.execute(f'SELECT * FROM wikipedia.{qident(wtable)} WHERE qid = ANY(%s) ORDER BY qid LIMIT 30', (qids,))
            allcols = [d[0] for d in cur.description]
            allrows = [list(r) for r in cur.fetchall()]
            if not allrows:                                                # nothing synced yet -> skip the (empty) slide
                return
            qi = allcols.index("qid") if "qid" in allcols else 0
            keep = [i for i, c in enumerate(allcols)                        # qid + columns that carry data, capped
                    if i == qi or any(row[i] not in (None, "") for row in allrows)][:8]
            cols = [allcols[i] for i in keep]                              # the faithful wikipedia columns (NO source col)
            rows = [[("" if r[i] is None else r[i]) for i in keep] for r in allrows]
            hlcol = "name" if "name" in cols else (cols[1] if len(cols) > 1 else None)   # entity-name column to highlight
            ctx_emit(f"resolve/{ridx[0]}", {"table": table, "column": column, "wtable": wtable,
                                            "columns": cols, "rows": rows, "hlcol": hlcol})
            ridx[0] += 1
        except Exception as e:                                             # noqa: BLE001 — streaming must never break the answer
            print("resolve slide skipped:", e, flush=True)

    def _emit_unconnected_slide(self, table, column, ridx):
        """Stream a resolution slide for a free-text (UNCONNECTED) string column: it's embedded into the unified-encoder
        vector bridge (no wikipedia table), so the client shows <table> with <column> highlighted + a 'Resolving… ->
        Resolved' note instead of a world table. Best-effort — streaming must never break the answer."""
        try:
            from engine.trace import ctx_emit
            ctx_emit(f"resolve/{ridx[0]}", {"table": table, "column": column, "unconnected": True})
            ridx[0] += 1
        except Exception as e:                                             # noqa: BLE001
            print("unconnected slide skipped:", e, flush=True)

    @staticmethod
    def _is_numeric(cells):
        """True iff every non-empty cell parses as a number (a measure/ID column, not a free-text string)."""
        import re as _re
        for c in cells:
            s = str(c).strip().lstrip("$").rstrip("%").replace(",", "")
            if s and not _re.match(r"^-?\d+(\.\d+)?$", s):
                return False
        return True

    def _enrich(self, cur, ent):
        """Fill each entity's attribute row: group by world_type, join its source table by world_key for that type's
        analytical columns, then chain any country -> continent + currency. Best-effort per type (a missing table is
        skipped, not fatal)."""
        by_type = {}
        for val, d in ent.items():
            by_type.setdefault(d.get("_wt"), []).append((val, d.get("_wk")))
        for wt, pairs in by_type.items():
            cfg = self.ENTITY_ATTRS.get(wt)
            keys = sorted({k for _, k in pairs if k})
            if not cfg or not keys:
                continue
            table, keycol, cols = cfg
            sel = ", ".join([f'"{keycol}"'] + [f'"{wc}"' for wc, _ in cols])
            try:
                cur.execute(f'SELECT {sel} FROM world."{table}" WHERE "{keycol}" = ANY(%s)', (keys,))
                by_key = {row[0]: row[1:] for row in cur.fetchall()}
            except Exception as e:                        # noqa: BLE001
                print(f"entity attrs skipped ({table}):", e, flush=True); continue
            for val, wk in pairs:
                for (_, name), v in zip(cols, by_key.get(wk, ())):
                    if v is not None:
                        ent[val][name] = v
        countries = sorted({d["country"] for d in ent.values() if d.get("country")})
        if countries:
            try:
                cur.execute('SELECT name, continent, currency_name FROM world."Countries" WHERE name = ANY(%s)',
                            (countries,))
                cc = {n: (co, cu) for n, co, cu in cur.fetchall()}
            except Exception as e:                        # noqa: BLE001
                print("continent/currency skipped:", e, flush=True); cc = {}
            for d in ent.values():
                co_cu = cc.get(d.get("country"))
                if co_cu:
                    if co_cu[0]:
                        d["continent"] = co_cu[0]
                    if co_cu[1]:
                        d["currency"] = co_cu[1]

    # the engine "did something" iff it built one of these; a plan that collapsed to just [join, group_agg] means the
    # gate misfired (e.g. 'German sales' bound no filter) -> defer to the delegate so its clarify gate can fire.
    _REAL_VIEWS = {"world_join", "world_filter", "topn", "yoy", "running", "share", "divide", "having",
                   "time_filter", "filter", "sort"}
    # the DEPTH composition views — the engine genuinely COMPOSED (vs a plain world-filtered aggregate that merely
    # MISFIRED the depth gate, e.g. a spurious primitive on "total customers in France" gating it to the engine,
    # whose plan then collapses to base join/world_join/world_filter/group_agg). Standing on the engine requires one
    # of these OR a world MEASURE the delegate can't expose; a bare aggregate/count defers to the delegate, which is
    # authoritative (incl. the entity-count fix) — so the gate's CPU-sensitive primitive readout is non-load-bearing
    # for plain aggregates (it can flip between hosts, but the answer no longer depends on it).
    _COMPOSITION_VIEWS = {"topn", "yoy", "running", "share", "divide", "having", "time_filter", "sort"}

    def _run_engine(self, tables, question, sub, as_of, emit=None):
        """Build the composed view stack (join -> world_join -> world_filter -> ... -> aggregate) for `question`
        and shape it into the serve response (views carry their own columns/rows so the UI can walk each step).
        `emit` streams the trace to RTDB live: status:resolving during the (slow) world lookup, then each view."""
        if emit:
            emit("status", "resolving")
        norm, _ = self.qw.ingest(tables)
        world = self._world_lookup(norm, sub)
        res = self.reason.run(tables, question, world=world)
        views = [{"name": v["name"], "op": v["op"], "label": v["label"], "sql": v["sql"],
                  "columns": v["columns"], "rows": [list(r) for r in v["rows"][:50]]} for v in res["views"]]
        if emit:
            emit("status", "running")
            for i, v in enumerate(views):
                emit(f"views/{i}", {k: v[k] for k in ("op", "label", "sql", "columns", "rows")})
        final = res["views"][-1] if res["views"] else None
        return {"question": question, "as_of": as_of, "error": None,
                "model": "engine - composed view stack",
                "plan": res["plan"], "primitives": res["primitives"], "views": views,
                "sql": final["sql"] if final else None,
                "result": res["answer"]}

    @staticmethod
    def _is_scalar(result):
        rows = (result or {}).get("rows") or []
        return len(rows) == 1 and len(rows[0]) == 1

    @classmethod
    def _same_answer(cls, a, b):
        """The re-expressed view stack must reproduce the delegate's scalar answer, else it doesn't stand."""
        if not (cls._is_scalar(a) and cls._is_scalar(b)):
            return False
        va, vb = a["rows"][0][0], b["rows"][0][0]
        try:
            return abs(float(va) - float(vb)) < 1e-6
        except (TypeError, ValueError):
            return str(va) == str(vb)

    def serve(self, tables, question, sub, as_of=None, emit=None):
        """Composed (composition primitives / COUNT / world MEASURE) -> the view stack. A plain world-FILTERED
        scalar aggregate ('total amount in France') is ALSO re-expressed as the view stack so the demo SHOWS the
        reasoning (resolve -> world join -> filter -> aggregate) instead of jumping to the number. The delegate
        (WorldQuery) stays AUTHORITATIVE for clarify / list / hybrid / non-geo / no-filter; the re-expression only
        stands when it reproduces the delegate's answer. Everything else delegates unchanged."""
        if self._composed(tables, question):
            try:
                er = self._run_engine(tables, question, sub, as_of, emit=emit)
                # Stand on the engine only if it actually built a composition/world stack. If it collapsed to a plain
                # [join, group_agg] (the gate misfired and no filter bound, e.g. 'German sales'), fall through to the
                # delegate so its CLARIFY gate fires instead of silently returning the ungrouped total.
                if (any(v.get("op") in self._COMPOSITION_VIEWS for v in (er.get("views") or []))
                        or self.WORLD_MEASURES.search(question or "")):
                    return er
            except Exception as e:                        # noqa: BLE001 — never hard-fail; fall back to delegate
                import traceback
                print("composed serve failed, delegating to WorldQuery:", e, flush=True)
                traceback.print_exc()
            return self.qw.serve(tables, question, as_of=as_of, schema=sub)
        # Not gated to the engine: the delegate owns clarify / list / hybrid / non-geo / plain answers.
        deleg = self.qw.serve(tables, question, as_of=as_of, schema=sub)
        # But a CLEAN world-filtered scalar aggregate (a world join in meaning_join, one number, NO clarify) is
        # re-expressed as the view stack so the reasoning is shown — kept only if it matches the delegate's answer.
        if (not deleg.get("clarify") and not deleg.get("error")
                and deleg.get("meaning_join") and self._is_scalar(deleg.get("result"))):
            try:
                er = self._run_engine(tables, question, sub, as_of, emit=emit)
                if er and self._same_answer(er.get("result"), deleg.get("result")):
                    return er
                print("view re-expression answer mismatch; keeping delegate", flush=True)
            except Exception as e:                        # noqa: BLE001 — re-expression is best-effort; never breaks the answer
                import traceback
                print("view re-expression failed, keeping delegate:", e, flush=True)
                traceback.print_exc()
        return deleg
