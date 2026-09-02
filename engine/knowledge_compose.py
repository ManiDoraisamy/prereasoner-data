"""ComposedKnowledgeQuery: the view-stacking COMPOSITION reasoner wired onto the live world path.

A world question with composition DEPTH (year-over-year, top-N, share, cumulative, ratio, sort) or more than
one uploaded table is answered by a STACK of views — the analytical primitives composed by depth
(engine.compose) over a BASE relation that is the uploaded FK join + the world-meaning join. A plain
aggregate / hybrid-semantic / clarify query DELEGATES to KnowledgeQuery unchanged. So composition ADDS depth
without regressing anything.

Reuse, not reinvention:
  * the WORLD lookup (city -> country) comes from KnowledgeQuery's world schema (knowledgebase."words"); the uploaded data
    is already in the request, so the only thing Postgres provides for the composed path is the meaning lookup.
  * the analytical DAG is the deterministic ComposeEngine (filter/time/group/yoy/running/share/divide/having/
    top-N/sort), driven by the LEARNED 10-primitive head on the SAME unified encoder.
  * operator + measure come from the metric space (read_op_model + cosine), like the rest of the program.

Auth (the verified Google sub) is enforced by the server; the composed path reads only the shared read-only
world schema + the request's own data (no per-user schema read -> no IDOR).
"""
from __future__ import annotations
import hashlib
import re
import threading

from engine.tables import qident
from engine.entities import WORLD_TABLE_TYPE
from engine.knowledge_query import KnowledgeQuery
from engine.primitive_head import PrimitiveReader
from engine.compose import ComposeEngine
from engine.routing import DEPTH_PRIMS, WORLD_MEASURES, compose_owns


class ComposedKnowledgeQuery:
    """Composition over the live multi-table + world base; delegates non-composed queries to KnowledgeQuery."""

    # Bridge tables are conversation-scoped mutable state. A small local lock stripe handles
    # threads sharing one process; a PostgreSQL advisory lock handles separate service
    # processes. The lock spans refresh and execution, so another request cannot observe the
    # bridge between DELETE and INSERT.
    _serve_locks = tuple(threading.RLock() for _ in range(64))

    # Routing constants (DEPTH_PRIMS = primitive-head EVIDENCE to build a compose plan; WORLD_MEASURES = a world
    # attribute the upload can't expose) and the routing AUTHORITY (compose_owns) live in engine.routing — the ONE
    # shared router used by both this serving path and the Spider eval, so they can never drift.
    def __init__(self):
        self.qw = KnowledgeQuery()                            # resolution + world DB + auth + bridge machinery
        self.reader = PrimitiveReader(encoder=self.qw)    # the learned 10-primitive head on the SAME unified encoder
        self.reason = ComposeEngine(reader=self.reader)   # the deterministic composition engine

    def _composed(self, tables, question):
        """LEARNED compose gate (no lexical DEPTH regex): route to the engine when the unified encoder READS a
        composition primitive (the head) or a COUNT intent (read_op_model), OR the question names a distinctive world
        MEASURE the delegate can't expose (population / atomic number). Plain SUM/AVG, bare group-by, list, and
        hybrid-semantic queries (no such readout) delegate to KnowledgeQuery — which itself distinguishes list from
        aggregate. So whether a question needs composition is mostly a MODEL readout, not a keyword list."""
        if WORLD_MEASURES.search(question or ""):
            return True
        try:
            # A bare COUNT no longer gates to the engine. The delegate's count+world path was strengthened (the
            # entity-count fix: "total/how many <entity> [in <place>]" -> COUNT via the qid world join), and a
            # spurious COUNT readout on a PROJECTION ("which continent is Kyoto in") would otherwise hijack it into
            # the engine's count. Counts WITH composition depth still gate via the DEPTH_PRIMS arm (e.g. top-N by
            # count); a plain count delegates to the fixed KnowledgeQuery, and serve() still re-expresses a clean
            # world-filtered scalar as the view stack (guarded by _same_answer) so the reasoning is still shown.
            return bool(self.reader.present(question) & DEPTH_PRIMS)
        except Exception as e:                            # noqa: BLE001 — the gate must never break the world path
            print("compose gate failed, delegating:", e, flush=True)
            return False

    # A question with a DATA-INTENT word, or that names a schema column/table, or whose token resolves to a
    # world entity, is a query OVER THE DATA. A question with NONE of these ("how does this work?") is almost
    # certainly conversational — route it to the Sonnet fallback instead of forcing a degenerate COUNT(*). This
    # is the COVERAGE gate for wholly-non-data questions (the clarify gate still handles AMBIGUOUS data ones).
    _DATA_INTENT = frozenset({
        "total", "sum", "average", "avg", "mean", "count", "number", "list", "show", "give", "display",
        "top", "most", "least", "largest", "smallest", "highest", "lowest", "biggest", "maximum", "minimum",
        "max", "min", "per", "each", "group", "grouped", "sort", "sorted", "order", "ordered", "rank",
        "ranked", "distinct", "unique", "where", "greater", "less", "between", "over", "under", "above",
        "below", "fewer", "filter"})
    _META_STOP = frozenset({
        "how", "does", "this", "that", "it", "work", "works", "the", "a", "an", "is", "are", "was", "were",
        "do", "did", "you", "your", "we", "our", "me", "i", "can", "could", "would", "should", "what", "why",
        "when", "who", "explain", "tell", "about", "mean", "means", "help", "and", "or", "but", "so"})

    def _has_data_signal(self, question, tables):
        """True iff the question looks like a query over the DATA (a data-intent word, a schema column/table
        mention, or a token that resolves to a world entity). False -> a conversational/meta question that the
        fallback should answer, not a forced degenerate query. Best-effort; on any error it FAILS OPEN (returns
        True — never block a real query). This is the OPPOSITE default from _human_tone, which fails closed."""
        try:
            ql = (question or "").lower()
            if re.search(r"\bhow\s+(?:many|much)\b", ql):
                return True                                       # 'how many/much' is a data intent (not 'how does…')
            toks = set(re.findall(r"[a-z]+", ql))
            if toks & self._DATA_INTENT:
                return True
            if toks & self._schema_tokens(tables):
                return True
            content = [w for w in toks if len(w) > 2 and w not in self._META_STOP]
            if content and self.qw._best_world_entity(content):
                return True
            return False
        except Exception:                                        # noqa: BLE001 — fail OPEN: never block a real query
            return True

    def _schema_tokens(self, tables):
        """The set of lowercased word-parts of every table/column name (plus a naive de-pluralized form)."""
        schw = set()
        for t in (tables or []):
            cols = t.get("columns") or [c.strip() for c in (str(t.get("data", "")).splitlines() or [""])[0].split(",")]
            for nm in [t.get("name", "")] + list(cols):
                for part in re.split(r"[^a-z0-9]+", str(nm).lower()):
                    if len(part) > 1:
                        schw.add(part); schw.add(part.rstrip("s"))
        return schw

    # An emotional / opinion / first-person-worry cue. When a question IS a real data query (an answer gets
    # computed) but is phrased like a human talking rather than a query spec, a bare number reads cold — we
    # route the computed answer + derivation through Sonnet to PRESENT it in human words. Cheap lexical test;
    # it fires only alongside a real answer, so an occasional false positive just means a warmer reply.
    # Deliberately EMOTIONAL words only. Generic adjectives that double as data values / status / intensifiers
    # ("really", "okay", "ok", "normal", "healthy", "please", "fine") are excluded — they'd fire on plain
    # queries ("rows where status = ok", "really big amounts"). Those still count as opinion INSIDE the
    # "is that/this/it <X>" regex below, where the framing makes them evaluative rather than a data value.
    _HUMAN_CUE = frozenset({
        "worried", "worry", "worrying", "concerned", "concern", "concerning", "hope", "hoping", "afraid",
        "scared", "nervous", "anxious", "stressed", "feel", "feeling", "felt", "believe", "think", "thinking",
        "wonder", "wondering", "curious", "love", "hate", "glad", "sad", "happy", "unhappy", "upset",
        "frustrated", "excited", "surprised", "disappointed", "relieved", "great", "terrible", "awful",
        "amazing", "wonderful", "honestly", "frankly", "confused", "struggling", "overwhelmed", "proud",
        "embarrassed"})
    _HUMAN_RE = re.compile(
        r"\b(i'?m|i am|we'?re|we are|i'?ve|should i|should we|do you think|what do you think|"
        r"is (?:that|this|it) (?:an? )?(?:good|bad|ok|okay|normal|healthy|fine|concerning|"
        r"problem|bad news)|too (?:high|low|expensive|cheap|slow|risky)|"
        r"am i|are we|is my|is our)\b", re.I)

    def _human_tone(self, question, tables):
        """True iff the phrasing carries an emotional / opinion / first-person cue — a cheap signal that a
        computed answer should be PRESENTED in human words rather than shown as a bare table. A cue word that
        is ALSO a schema column/table name ("show me the feeling column") is a DATA reference, not emotion, so
        it doesn't count. The evaluative "is that/this/it <X>" framing counts regardless. Best-effort; on any
        error it FAILS CLOSED (returns False — never force a needless present). Opposite of _has_data_signal."""
        try:
            ql = (question or "").lower()
            if self._HUMAN_RE.search(ql):
                return True
            cues = set(re.findall(r"[a-z']+", ql)) & self._HUMAN_CUE
            if not cues:
                return False
            return bool(cues - self._schema_tokens(tables))      # a cue that names a column is data, not tone
        except Exception:                                        # noqa: BLE001
            return False

    # The analytical attributes of each resolved-entity TYPE: (source table, key column, [(world_col, exposed_name)]).
    # The bridge gives world_type + world_key (qid for a city, canonical name otherwise); we join the source table by
    # that key to expose the FULL resolved-entity row (population / atomic_number / mass / ...), not just country.
    # Entities that have a country are additionally chained country -> continent + currency (knowledgebase."Countries").
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
                        # Stream ONE 'Resolving <col>' slide (this column's knowledgebase."<type>" rows) LIVE, ahead of the
                        # view stack. Reads the persisted bridge, so it fires fresh OR cached; uses the server's emit
                        # CONTEXT (set under its LOCK), so _world_lookup needn't thread `emit` through.
                        self._emit_resolve_slide(cur, t["name"], col, col_rows, _ridx)
                    except Exception as e:                # noqa: BLE001 — a column that won't resolve is skipped
                        print("world lookup skipped", t["name"], col, ":", e, flush=True)
                elif not self._is_numeric(cells):         # UNCONNECTED free-text string column -> the embedding slide
                    self._emit_unconnected_slide(t["name"], col, _ridx)
            if ent and result is None:
                self._enrich(cur, ent)                    # join each entity's source table + country -> continent/currency
                # drop an enriched attr whose name collides with the geo column itself (a COUNTRY column enriches a
                # 'country' attr -> two 'country' columns -> ComposeEngine._load crashed 'duplicate column name').
                attrs = sorted({k for d in ent.values() for k in d if not k.startswith("_") and k != geocol})
                result = {"name": "world meaning", "columns": [geocol] + attrs,
                          "rows": [[v] + [d.get(a) for a in attrs] for v, d in ent.items()]}
        return result

    def _emit_resolve_slide(self, cur, table, column, col_rows, ridx):
        """Stream ONE resolution slide for a CONNECTED string column: the knowledgebase."<type>" rows (qid + the faithful
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
                cur.execute('SELECT label FROM knowledgebase."types" WHERE qid=%s', (tq,))
                _r = cur.fetchone()
                if _r and _r[0]:
                    wtable = str(_r[0])[:63]
            cur.execute(f'SELECT * FROM knowledgebase.{qident(wtable)} WHERE qid = ANY(%s) ORDER BY qid LIMIT 30', (qids,))
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
                cur.execute(f'SELECT {sel} FROM knowledgebase."{table}" WHERE "{keycol}" = ANY(%s)', (keys,))
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
                cur.execute('SELECT name, continent, currency_name FROM knowledgebase."Countries" WHERE name = ANY(%s)',
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

    def _run_engine(self, tables, question, sub, as_of, emit=None, world=None):
        """Build the composed view stack (join -> world_join -> world_filter -> ... -> aggregate) for `question`
        and shape it into the serve response (views carry their own columns/rows so the UI can walk each step).
        `emit` streams the trace to RTDB live: status:resolving during the (slow) world lookup, then each view.
        When `world` is passed (serve() hoisted the lookup to route on world-grounding) the caller has ALREADY
        emitted status:resolving before that lookup, so we don't re-emit it here."""
        if world is None:                                     # standalone call (the re-expression path): do our own
            if emit:                                          # lookup and stream resolving around it.
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
                "world_dependency": res.get("world_dependency"),
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
            return parse_decimal(va, enforce_input_bounds=False) == parse_decimal(
                vb, enforce_input_bounds=False,
            )
        except (TypeError, ValueError):
            return str(va) == str(vb)

    def serve(self, tables, question, sub, as_of=None, emit=None, explicit_fks=()):
        if not sub:
            return self._serve_locked(tables, question, sub, as_of=as_of, emit=emit,
                                      explicit_fks=explicit_fks)
        digest = hashlib.sha256(str(sub).encode("utf-8")).digest()
        local_lock = self._serve_locks[int.from_bytes(digest[:2], "big") % len(self._serve_locks)]
        with local_lock:
            conn = self.qw._rconn()
            cur = conn.cursor()
            try:
                cur.execute("SELECT pg_advisory_lock(hashtext(%s), hashtext(%s))",
                            ("prereasoner-world-bridge", str(sub)))
                try:
                    return self._serve_locked(tables, question, sub, as_of=as_of, emit=emit,
                                              explicit_fks=explicit_fks)
                finally:
                    cur.execute("SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
                                ("prereasoner-world-bridge", str(sub)))
            finally:
                cur.close()

    def _serve_locked(self, tables, question, sub, as_of=None, emit=None, explicit_fks=()):
        """Composed (composition primitives / COUNT / world MEASURE) -> the view stack. A plain world-FILTERED
        scalar aggregate ('total amount in France') is ALSO re-expressed as the view stack so the demo SHOWS the
        reasoning (resolve -> world join -> filter -> aggregate) instead of jumping to the number. The delegate
        (KnowledgeQuery) stays AUTHORITATIVE for clarify / list / hybrid / non-geo / no-filter; the re-expression only
        stands when it reproduces the delegate's answer. Everything else delegates unchanged."""
        if explicit_fks:
            return self.qw.serve(tables, question, as_of=as_of, schema=sub,
                                 explicit_fks=explicit_fks)
        if self._composed(tables, question):
            # _composed is EVIDENCE (primitive-head / world-measure cue) that a compose plan is worth building —
            # never authority to stand on it. Ground the world dependency FIRST: if nothing resolves against the
            # world model, the query is answerable from the uploaded tables alone, so the typed-AST planner owns it
            # and we never build (let alone stand on) the ComposeEngine. Only when a world dependency grounds do we
            # build the view stack and let engine.routing.compose_owns decide on the EXPLICIT world_dependency
            # record (a genuine, NECESSARY world composite stands; a redundant join or a plain world lookup falls
            # through to the authoritative delegate). The world lookup is the slow step, so stream resolving first.
            if emit:
                emit("status", "resolving")
            norm, _ = (self.qw.ingest(tables, explicit_fks=explicit_fks)
                       if explicit_fks else self.qw.ingest(tables))
            world = self._world_lookup(norm, sub)
            if world:
                try:
                    er = self._run_engine(tables, question, sub, as_of, emit=emit, world=world)
                    if compose_owns(er.get("views"), er.get("world_dependency"),
                                    (er.get("result") or {}).get("rows")):
                        return er
                except Exception as e:                    # noqa: BLE001 — never hard-fail; fall back to delegate
                    import traceback
                    print("composed serve failed, delegating to KnowledgeQuery:", e, flush=True)
                    traceback.print_exc()
            return (self.qw.serve(tables, question, as_of=as_of, schema=sub,
                                  explicit_fks=explicit_fks)
                    if explicit_fks else self.qw.serve(tables, question, as_of=as_of, schema=sub))
        # Not gated to the engine: the delegate owns clarify / list / hybrid / non-geo / plain answers.
        deleg = (self.qw.serve(tables, question, as_of=as_of, schema=sub,
                               explicit_fks=explicit_fks)
                 if explicit_fks else self.qw.serve(tables, question, as_of=as_of, schema=sub))
        # A KNOWLEDGEBASE-converted answer already carries its own richer stack (the joined ->
        # filtered -> per-row `calculated` views with the exact rate and its publication date) —
        # compose's re-expression cannot show the rate step, so the delegate wins and its views
        # stream like compose's would. The resolution slides stream the same way as on the compose
        # path: _world_lookup re-walks the bridges the delegate's serve just built (cached Postgres
        # reads) and ctx_emits one knowledgebase slide per connected column. Best-effort.
        if (deleg.get("currency") or {}).get("realization") == "converted" and deleg.get("views"):
            try:
                norm, _ = self.qw.ingest(tables)
                self._world_lookup(norm, sub)
            except Exception as e:                        # noqa: BLE001 — slides never break the answer
                print("resolution slides skipped:", e, flush=True)
            if emit:
                for i, v in enumerate(deleg.get("views") or []):
                    emit(f"views/{i}", {k: v[k] for k in ("op", "label", "sql", "columns", "rows")})
            return deleg
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
from engine.numeric import parse_decimal
