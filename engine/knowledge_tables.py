"""Relational attention from an uploaded CSV to the IMPLICIT world meaning tables.

A CSV's city column is ROUTED to the city word-table by its concept (read off the anchored dims) and joined
on the NORMALIZED string — concept picks the TABLE, string picks the ROW. A filter value that is ABSENT from
the uploaded data ("France") is resolved by finding it in a word-table's ATTRIBUTE (`city.country`) and
BUILDING the join — the implicit table made explicit. Your table holds only scalars; meaning + freshness
(`updated_at`/`source`) live in the world tables. Everything stays a SELECT.

This layer is deterministic planning on top of the encoder/readout (no new training): it defines the query
class — `SELECT … FROM csv JOIN city ON lower(csv.city)=lower(city.name) WHERE city.country = 'France'` —
that the live Postgres layers (engine.pg / engine.entities) then execute against the real world DB.
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3

from engine.config import DATA_DIR
from engine.currency_intent import (
    currency_rate_binding, is_currency_measure_column, is_currency_source_column,
)
from engine.numeric import coerce_numeric, register_sqlite_decimal, sqlite_numeric, wire_rows
from engine.tables import (  # noqa: F401  (csv_table re-exported)
    TableQuery,
    csv_table,
    name_words,
    qident,
    qlit,
    wmatch,
)

WORD_DIR = DATA_DIR
CITY_CONCEPTS = {"city", "place", "location", "municipality", "urban_area", "urban area",
                 "geographical_area", "geographical area", "region", "district", "town"}
# SUM cues include the MONEY/MEASURE nouns (sales, revenue, …): the model tags amount/sales/revenue/price all as
# is_num (one KIND — a numeric measure), so a measure noun implies a total over the is_num column even when it does
# NOT lexically match the column name ("sales" -> the `amount` column). read_op_all resolves the target by datatype.
MEASURE_NOUNS = {"sales", "revenue", "revenues", "turnover", "spend", "spending", "amount", "amounts",
                 "cost", "costs", "price", "prices", "value"}
# a question may ask to SELECT/AGGREGATE a column of the world DB itself (not the upload): map the word -> the world
# column. The planner enumerates the reachable world-table columns and matches by name/synonym (so it KNOWS them, not guesses).
WORLD_COL_SYN = {"currency": "currency", "currencies": "currency", "population": "population",
                 "populations": "population", "inhabitants": "population", "continent": "continent",
                 "continents": "continent", "hemisphere": "hemisphere", "country": "country",
                 "countries": "country", "latitude": "lat", "longitude": "lng"}
WORLD_SKIP_COLS = {"name", "is_primary", "updated_at", "source", "source_release_id",
                   "valid_from", "valid_to"}
ARGMAX_CUES = frozenset({"highest", "largest", "most", "maximum", "max", "top"})
ARGMIN_CUES = frozenset({"lowest", "smallest", "least", "minimum", "min", "bottom"})
AGG_CUES = {"COUNT": {"count", "counts", "number", "many"}, "SUM": {"sum", "total", "totals"} | MEASURE_NOUNS,
            "AVG": {"avg", "average", "mean", "averages"}}
STALE_DAYS = 730                                          # a fact last verified > ~2y before the decision = stale
# every rate_to_<code> column the physical knowledgebase."exchange_rate" table carries
# (db/sync/build_exchange_rate.py builds one per ECB series + EUR)
_ECB_CODES = frozenset({
    "AUD", "BGN", "BRL", "CAD", "CHF", "CNY", "CYP", "CZK", "DKK", "EEK", "EUR", "GBP", "HKD",
    "HRK", "HUF", "IDR", "ILS", "INR", "ISK", "JPY", "KRW", "LTL", "LVL", "MTL", "MXN", "MYR",
    "NOK", "NZD", "PHP", "PLN", "ROL", "RON", "RUB", "SEK", "SGD", "SIT", "SKK", "THB", "TRL",
    "TRY", "USD", "ZAR",
})

# the world-knowledge tables in generated SQL are qid-keyed projections in the `knowledgebase` schema, named by the
# EXACT Wikidata label (knowledgebase."city" / knowledgebase."country"). The planner's logical slugs
# (word_city/word_country) remap here; the place hierarchy is dropped (no faithful Wikidata "place" type).
# The serving search_path includes `knowledgebase`, so bare names resolve.
# word_city/word_country -> qid-keyed knowledgebase."<type>" tables. word_state -> the aggregate qid-keyed
# knowledgebase."u_s_state" (built by db/sync/build_u_s_state.py; state qid PK, country/continent qid FKs) — so a
# state column joins qid-keyed and filters by country/continent, same as city/country. word_element still
# uses the friendly name-keyed family. The naming families are documented in docs/notes/naming.md.
WORLD_NAMES = {"word_city": "city", "word_country": "country", "word_state": "u_s_state",
               "word_exchange_rate": "exchange_rate"}


def _friendly(t):
    return WORLD_NAMES.get(t, t)


def load_word_tables():
    out = {}
    for fp in sorted(WORD_DIR.glob("word_*.json")):
        d = json.loads(fp.read_text(encoding="utf-8"))
        d["table"] = _friendly(d["table"])                # remap to the descriptive name everywhere it flows
        if d.get("parent"):
            d["parent"] = _friendly(d["parent"])
        for link in d.get("links", []):
            link["to_table"] = _friendly(link["to_table"])
        out[d["table"]] = d
    return out


class KnowledgeTableQuery:
    @staticmethod
    def _numeric_aggregate(function, operand):
        rendered = {
            "SUM": "decimal_sum", "AVG": "decimal_avg",
            "MIN": "decimal_min", "MAX": "decimal_max",
        }.get(function.upper(), function.upper())
        return f"{rendered}( {operand} )"

    @staticmethod
    def _numeric_multiply(left, right):
        return f"decimal_mul({left}, {right})"

    def __init__(self, deploy_dir=DATA_DIR):
        self.q11 = TableQuery(deploy_dir)         # the anchored readout planner (encoder overlaid by the world layer)
        self.words = load_word_tables()           # metadata only (key/concept/filter_attrs/filter_values/links/parent)
        self.dbpath = WORD_DIR / "words.db"       # OPTIONAL local SQLite word tables (the live path uses Postgres)

    def route(self, table):
        """concept routing by the cell VALUES' entity class (Paris -> location/region/district), so a column routes
        by what the model understands its values to BE — not its name. Where the anchored readout earns its keep."""
        base = table["name"]
        routes = {}
        if _friendly("word_city") not in self.words:
            return routes
        an = self.q11.analyze(table)
        cols = an["cols"]
        hits = {c: [0, 0] for c in cols}                       # per column: [city-like cells, non-empty cells]
        for row in an["rows"]:
            for ci, cell in enumerate(row["cells"]):
                evo = cell.get("evolution") or []
                if not evo:
                    continue
                hits[cols[ci]][1] += 1
                fin = evo[-1]
                top = [dn[4:] for dn, v in sorted(fin.items(), key=lambda kv: -kv[1]) if dn.startswith("ace_") and v >= 0.4]
                if any(t in CITY_CONCEPTS for t in top[:3]):
                    hits[cols[ci]][0] += 1
        for c in cols:
            city_like, n = hits[c]
            if c.lower() in CITY_CONCEPTS or (n >= 2 and city_like / n >= 0.5):
                routes[(base, c)] = _friendly("word_city")
        return routes

    def column_dims(self, sch, base):
        """per uploaded column: the NAMED DIMENSIONS for the hover tags — value-sniffed datatype (so `sales` is a
        number, not the name reading as text) + the anchored entity-class meaning."""
        out = {}
        for c in sch:
            if c["table"] != base:
                continue
            dt = "is_num" if c["affinity"] in ("INTEGER", "REAL") else ("is_time" if "is_time" in c["struct"] else "is_str")
            out[c["name"]] = [dt] + ["ace_" + a.replace(" ", "_") for a in c.get("ace", [])]
        return out

    def _find_value(self, low_q, w):
        """does a filter-attr value of word table `w` appear in the question? Uses the PRECOMPUTED distinct
        `filter_values` (so we never scan 200k rows). Longest match wins; tolerant of a trailing plural 's'."""
        vals = {}
        for attr in w.get("filter_attrs", []):
            for v in w.get("filter_values", {}).get(attr, []):
                if v:
                    vals.setdefault(str(v).lower(), (attr, str(v)))
        for vl, (attr, vorig) in sorted(vals.items(), key=lambda kv: -len(kv[0])):
            if re.search(r"(?<![a-z])" + re.escape(vl) + r"s?(?![a-z])", low_q):
                return attr, vorig
        return None

    def meaning_filter(self, question, routes):
        """resolve a filter value ABSENT from the upload by walking the meaning graph (BFS, shortest path first):
        csv.col -> city -> (city.country) -> country -> ... . Returns the JOIN CHAIN + the (table, attr, value)
        that matched. 'France' resolves in 1 hop (city.country); 'euros' / 'Europe' in 2 hops
        (city -> country.currency / .continent)."""
        low_q = " " + question.lower() + " "
        for (t, col), wt0 in routes.items():
            start = {"left_table": t, "left_col": col, "right_table": wt0, "right_col": self.words[wt0]["key"]}
            frontier, seen = [(wt0, [start])], {wt0}
            while frontier:
                wt, path = frontier.pop(0)
                hit = self._find_value(low_q, self.words[wt])
                if hit:
                    return {"csv_table": t, "csv_col": col, "joins": path,
                            "filter_table": wt, "attr": hit[0], "value": hit[1]}
                for link in self.words[wt].get("links", []):
                    tt = link["to_table"]
                    if tt in self.words and tt not in seen:
                        seen.add(tt)
                        frontier.append((tt, path + [{"left_table": wt, "left_col": link["col"],
                                                       "right_table": tt, "right_col": link["to_col"]}]))
        return None

    @staticmethod
    def _own_value_matches(question, norm):
        """non-numeric values already PRESENT in the upload that the question quotes (word-boundary phrase).
        -> [(table, col, value)] deduped per (col, value). Used to (a) prefer own data when the question's filter
        value lives in the upload, and (b) AND extra own filters into a world join ("GOLD customers in France")."""
        q = " " + question.lower() + " "
        out, seen = [], set()
        for t in norm:
            cols = t["columns"]
            for r in t["rows"]:
                for ci, v in enumerate(r):
                    s = str(v).strip() if v is not None else ""
                    sl = s.lower()
                    if len(sl) < 3 or sl.replace(".", "").isdigit() or (cols[ci], sl) in seen:
                        continue
                    if re.search(r"(?<![a-z0-9])" + re.escape(sl) + r"(?![a-z0-9])", q):
                        seen.add((cols[ci], sl)); out.append((t["name"], cols[ci], s))
        return out

    def disambiguator(self, sch, base, routed_col, wt):
        """a homonym (two 'Paris' rows) is pinned when the UPLOAD itself carries a column whose values land in a
        word-table attribute (e.g. an uploaded `country` column). Returns (csv_col, word_attr) to AND into the join."""
        w = self.words[wt]
        for attr in list(w.get("filter_attrs", [])):
            wvals = {str(v).lower() for v in w.get("filter_values", {}).get(attr, []) if v}
            for c in sch:
                if c["table"] != base or c["name"] == routed_col or c["affinity"] != "TEXT":
                    continue
                cv = [str(v).lower() for v in c["values"] if v not in (None, "")]
                if cv and sum(v in wvals for v in cv) / len(cv) >= 0.5:
                    return c["name"], attr
        return None

    def ambiguities(self, table, routed_col, wt):
        """upload values that match MORE THAN ONE word-table row and were NOT disambiguated -> flag them (queries
        the DB for just the uploaded values). The discrepancy-review discipline: answer, but say it's ambiguous."""
        w = self.words[wt]; key = w["key"]
        haskind = "country" in w.get("columns", [])
        vals = [str(dict(zip(table["columns"], r)).get(routed_col)) for r in table["rows"]]
        vals = [v for v in vals if v and v != "None"]
        if not vals:
            return []
        con = sqlite3.connect(self.dbpath)
        warns, seen = [], set()
        for v in vals:
            vl = v.lower()
            if vl in seen:
                continue
            seen.add(vl)
            if haskind:
                opts = [r[0] for r in con.execute(f"SELECT DISTINCT country FROM {qident(wt)} WHERE lower({qident(key)})=?", (vl,)).fetchall()]
                if len(opts) > 1:
                    warns.append(f"'{v}' is ambiguous in {wt}: {', '.join(sorted(opts))}")
            else:
                cnt = con.execute(f"SELECT COUNT(*) FROM {qident(wt)} WHERE lower({qident(key)})=?", (vl,)).fetchone()[0]
                if cnt > 1:
                    warns.append(f"'{v}' is ambiguous in {wt}: {cnt} rows")
        con.close()
        return warns

    def _uploaded_from(self, involved, fks):
        """FROM the involved uploaded sheets, chained by their discovered foreign keys. IDs join with plain
        equality (no lower()). Returns the exact selected FK records for calculation evidence."""
        inc = [involved[0]]; clauses, descs, selected = [], [], []
        remaining = [t for t in involved[1:] if t != involved[0]]
        progress = True
        while remaining and progress:
            progress = False
            for r in list(remaining):
                fk = next((f for f in fks if (f["from_table"] == r and f["to_table"] in inc)
                           or (f["to_table"] == r and f["from_table"] in inc)), None)
                if fk:
                    from_cols = tuple(fk.get("from_cols") or (fk["from_col"],))
                    to_cols = tuple(fk.get("to_cols") or (fk["to_col"],))
                    predicates = tuple(
                        f'{qident(fk["from_table"])}.{qident(left)} = '
                        f'{qident(fk["to_table"])}.{qident(right)}'
                        for left, right in zip(from_cols, to_cols)
                    )
                    clauses.append(f'JOIN {qident(r)} ON ' + " AND ".join(predicates))
                    descs.append(" AND ".join(
                        f'{fk["from_table"]}.{left} = {fk["to_table"]}.{right}'
                        for left, right in zip(from_cols, to_cols)
                    ))
                    selected.append(fk)
                    inc.append(r); remaining.remove(r); progress = True
        return (
            f'FROM {qident(involved[0])}' + ((' ' + ' '.join(clauses)) if clauses else ''),
            descs,
            inc,
            selected,
        )

    def _world_rate_binding(self, question, agg, sch):
        """Bind the conversion to knowledgebase."exchange_rate" when the upload has no rate sheet.

        The knowledgebase table joins exactly like a tenant table — conversation + tenant +
        knowledgebase is the ONE join shape — on the fact table's (currency, date) pair. Requires an
        OUTPUT-kind currency intent, a monetary SUM, a currency-code column and a date column on the
        fact table, and the exchange_rate table registered in the words index. Uploaded rate sheets
        always win: this is only consulted after _currency_conversion_binding returns None.
        """
        from engine.currency_intent import CurrencyIntentKind, currency_intent

        if "exchange_rate" not in getattr(self, "words", {}):
            return None                              # no registry (hermetic stub) = no knowledgebase tables
        if (not agg or agg[0] != "SUM" or not agg[1] or not agg[2]
                or not is_currency_measure_column(agg[2])):
            return None
        intent = currency_intent(question)
        if intent is None or intent.kind != CurrencyIntentKind.OUTPUT:
            return None
        fact = agg[1]
        ccy_col = next((c["name"] for c in sch if c["table"] == fact
                        and is_currency_source_column(c["name"])), None)
        # A dated fact table joins the rate of each row's own date; an undated one pins the request's
        # as_of date — the same bitemporal semantics every world join already uses (_join_cond).
        date_col = next((c["name"] for c in sch if c["table"] == fact and c.get("is_date")), None)
        if not ccy_col:
            return None
        rate_col = f"rate_to_{intent.target.lower()}"
        if rate_col not in [c.lower() for c in self.words["exchange_rate"].get("columns", [])]                 and intent.target.upper() not in _ECB_CODES:
            return None
        return {"fact": fact, "ccy_col": ccy_col, "date_col": date_col,
                "rate_col": rate_col, "target": intent.target.upper()}

    @staticmethod
    def _currency_conversion_binding(question, agg, sch, fks):
        """Find the one direct-rate column that can convert a world-filtered SUM."""
        if (not agg or agg[0] != "SUM" or not agg[1] or not agg[2]
                or not is_currency_measure_column(agg[2])):
            return None
        return currency_rate_binding(
            question,
            agg[1],
            (
                (
                    str(fk.get("from_table", "")),
                    str(fk.get("from_col", "")),
                    str(fk.get("to_table", "")),
                    str(fk.get("to_col", "")),
                )
                for fk in fks
            ),
            (
                (str(column["table"]), str(column["name"]))
                for column in sch
                if column.get("affinity") in ("INTEGER", "REAL")
            ),
        )

    def read_op_all(self, question, sch):
        """an aggregate (cue + a numeric MEASURE searched across ALL uploaded sheets, excluding key/id cols).
        A SUM/AVG cue followed by a SHEET noun and no measure ("total CUSTOMERS …") means COUNT that sheet's rows."""
        low = question.lower().split()
        numeric = [c for c in sch if c["affinity"] in ("INTEGER", "REAL") and not re.search(r"(^id$|_?id$)", c["name"].lower())]
        tnames = sorted({c["table"] for c in sch})

        def table_noun(start):
            return next((t for w in low[start:] for t in tnames
                         if w == t or w == t + "s" or w.rstrip("s") == t.rstrip("s")), None)

        for fn, cues in AGG_CUES.items():
            ci = next((i for i, t in enumerate(low) if t in cues), -1)
            if ci < 0:
                continue
            if fn == "COUNT":                            # count the ROWS of the named sheet (e.g. "how many ORDERS") so it
                return ("COUNT", table_noun(0), None)    # gets joined in — else COUNT(*) counts the wrong table
            tgt = None
            for k in range(ci + 1, len(low)):
                tgt = next((c for c in numeric if c["name"].lower() == low[k] or low[k] in c["name"].lower()), None)
                if tgt:
                    break
            if tgt is None:
                ct = table_noun(ci + 1)                  # "total customers in France" counts customers, not SUM(amount)
                if ct:
                    return ("COUNT", ct, None)
            tgt = (tgt or next((c for c in numeric if c["name"].lower() in MEASURE_NOUNS), None)   # "sales" -> the
                   or (numeric[0] if numeric else None))                                          # is_num measure col
            if tgt:
                return (fn, tgt["table"], tgt["name"])
        return None

    @staticmethod
    def _world_aff(col):
        return "INTEGER" if col in ("population", "is_primary") else ("REAL" if col in ("lat", "lng") else "TEXT")

    def world_target(self, question, routes):
        """Did the question ask for a WORLD column (population / currency / continent / hemisphere) — one that is NOT in
        the upload? BFS the reachable word-table graph from each routed city column, matching question words to column
        names (+ a small synonym map). Returns {table, col, affinity, path} so serve can join that table in and select it."""
        low = question.lower().replace("?", "").split()
        requested = [(WORLD_COL_SYN[w], w) for w in low if w in WORLD_COL_SYN]
        # Some dimensions have meaningful compound names. Treat the phrase as a
        # unit so "atomic mass" cannot be captured by the prefix "atomic" as
        # atomic_number, and keep the surface word for the response metadata.
        compact = " ".join(low)
        for phrase, column in (("atomic number", "atomic_number"), ("atomic mass", "mass")):
            if re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", compact):
                requested.append((column, phrase.split()[-1]))
        want = {column for column, _word in requested}
        if not want:
            return None
        for (t, col0), wt0 in routes.items():
            start = {"left_table": t, "left_col": col0, "right_table": wt0, "right_col": self.words[wt0]["key"]}
            frontier, seen = [(wt0, [start])], {wt0}
            while frontier:
                wt, path = frontier.pop(0)
                for c in self.words[wt].get("columns", []):
                    if c not in WORLD_SKIP_COLS and c in want:
                        word = next(word for column, word in requested if column == c)
                        return {"table": wt, "col": c, "affinity": self._world_aff(c), "path": path, "word": word}
                for link in self.words[wt].get("links", []):
                    tt = link["to_table"]
                    if tt in self.words and tt not in seen:
                        seen.add(tt)
                        frontier.append((tt, path + [{"left_table": wt, "left_col": link["col"],
                                                       "right_table": tt, "right_col": link["to_col"]}]))
        return None

    def _world_rows(self, joins, seed_values, cap=12):
        """fetch ONLY the participating world rows (value context, not a DB dump): walk the join chain, seeding each
        world table from the previous table's join-key values — the first from the uploaded city values."""
        con = sqlite3.connect(self.dbpath)
        out, seeds = [], [str(v).lower() for v in seed_values if v not in (None, "")]
        for idx, j in enumerate(joins):
            wt, key, w = j["right_table"], j["right_col"], self.words[j["right_table"]]
            cols = [c for c in w["columns"] if c not in ("updated_at", "source", "valid_from", "valid_to")]
            seedset = sorted(set(seeds))
            if not seedset:
                break
            ph = ",".join("?" * len(seedset))
            extra = " AND is_primary=1" if "is_primary" in w["columns"] else ""
            rows = con.execute(f'SELECT {", ".join(qident(c) for c in cols)} FROM {qident(wt)} '
                               f'WHERE lower({qident(key)}) IN ({ph}){extra} LIMIT {cap}', tuple(seedset)).fetchall()
            rd = [[("" if v is None else v) for v in r] for r in rows]
            out.append({"name": wt, "columns": cols, "rows": rd})
            nxt = joins[idx + 1] if idx + 1 < len(joins) else None
            if nxt and nxt["left_table"] == wt and nxt["left_col"] in cols:
                ci = cols.index(nxt["left_col"]); seeds = [str(r[ci]).lower() for r in rd if r[ci] not in (None, "")]
            else:
                seeds = []
        con.close()
        return out

    def world_encode(self, joins, seed_values):
        """run the PARTICIPATING world rows through the SAME model (a table-name unit + column names + cell values) so
        the world table is GENUINELY encoded, not faked. Returns (debug sections WITH rows, dims for the SQL hover)."""
        sections, dims = [], {}
        for ws in self._world_rows(joins, seed_values):
            wt, cols, rows = ws["name"], ws["columns"], ws["rows"]
            an = self.q11.analyze({"name": wt, "columns": cols, "rows": rows}, table_unit=True)   # <-- sent to the model
            for ci, c in enumerate(cols):
                evo = an["columns"][ci]["evolution"]; fin = evo[-1] if evo else {}
                ace = [k for k in fin if k.startswith("ace_")][:4]
                dt = "is_num" if self._world_aff(c) in ("INTEGER", "REAL") else "is_str"
                dims[f"{wt}.{c}"] = [dt] + ace
            tfin = (an.get("table_evolution") or [{}])[-1]
            dims[wt] = ([k for k in tfin if k.startswith("ace_")][:5]) or ["is_str"]   # the table-name unit's own dims
            sections.append({"name": wt, "kind": "world", "lines": [cols] + rows})
        return sections, dims

    def _debug_input(self, norm, question, world_sections, features, plan, encoded_q):
        """The FAITHFUL input→output flow for the Debug tab, as stages: INPUT (your sheets + the question) → DERIVED
        (the world rows the routing pulled in) → FEATURES (the model's last-layer dims) → PLAN (slot decisions, each
        tagged model-feature vs deterministic-rule) → OUTPUT (the SQL the template fills)."""
        MAXV = 24
        inp = []
        for t in norm:
            lines = [[str(c) for c in t["columns"]]] + [[("" if v is None else str(v)) for v in r] for r in t["rows"][:MAXV]]
            inp.append({"name": t["name"], "lines": lines})
        return {"input": inp, "question": question.split(), "derived": world_sections,
                "features": features, "plan": plan, "encoded_question": encoded_q}

    def _subject_col(self, question, mtab, route_col, sch, routes):
        """When the question's SUBJECT *is* the routed concept ("CITIES in US"), the column to project is the
        ROUTED column itself (the City column that joined to the world table) — NOT the positional "first text
        column", which on a lat/long sheet (LatD,LatM,LatS,NS,…,City,State) wrongly picks a junk lead column
        ("NS" = hemisphere). Returns route_col when the question names the routed concept, else None (keep the
        first-text-column default, so "customers in France" still projects the customer name, not its city)."""
        friendly = routes.get((mtab, route_col))
        if not friendly:
            return None
        if not any(c["table"] == mtab and c["name"] == route_col and c["affinity"] == "TEXT" for c in sch):
            return None                                          # only project the routed col if it's text
        concept = friendly.split()[0].lower()                    # "cities" / "states" / "countries" / "elements"
        sing = concept[:-3] + "y" if concept.endswith("ies") else concept.rstrip("s")   # cities->city, states->state
        ql = question.lower()
        if re.search(r"(?<![a-z])(" + re.escape(concept) + "|" + re.escape(sing) + r")(?![a-z])", ql):
            return route_col                                     # the concept word ("cities"/"city") is the subject
        if any(re.search(r"(?<![a-z])" + re.escape(w.lower()) + r"(?![a-z])", ql) for w in name_words(route_col)):
            return route_col                                     # …or the routed column's own name appears
        return None

    def _build_plan(self, proj_desc, sheet_joins, world_joins, mf, own_filters):
        """the slot decisions, in SQL order, FAITHFUL to the assembled SQL: only the joins/filters actually in it.
        Each tagged kind=model (a feature drove it) or rule (deterministic)."""
        plan = [{"slot": proj_desc[0], "value": proj_desc[1], "via": proj_desc[2], "kind": "rule"}]
        for d in sheet_joins:
            plan.append({"slot": "join · sheets", "value": d, "via": "FK discovery (engine.relations)", "kind": "rule"})
        for idx, j in enumerate(world_joins):
            plan.append({"slot": "join · world", "value": f'{j["left_table"]}.{j["left_col"]} → {j["right_table"]}',
                         "via": ("values fire ace_city → routed" if idx == 0 else "meaning-graph link"),
                         "kind": ("model" if idx == 0 else "rule")})
        if mf:
            plan.append({"slot": "filter", "value": f'{mf["filter_table"]}.{mf["attr"]} = {qlit(mf["value"])}',
                         "via": f'"{mf["value"]}" matched in {mf["attr"]} values', "kind": "rule"})
        for (t, c, v) in own_filters:
            plan.append({"slot": "filter", "value": f'{t}.{c} = {qlit(v)}',
                         "via": "value found in your sheet", "kind": "rule"})
        return plan

    def _world_joins(self, upfrom, joins, sch, norm, mtab, route_col, as_of, mf=None):
        """Assemble the meaning JOIN chain (city -> word city table -> country -> …). The idx==0 join is the
        CELL-side match (uploaded value -> world key); engine.entities overrides this to route idx==0 through a
        resolved bridge (and uses `mf`, the meaning filter, for context-aware same-name disambiguation). Default
        ignores `mf` and reproduces the original SQL exactly. -> (from_sql, disamb, warns)."""
        fw = upfrom
        wt0 = joins[0]["right_table"]
        disamb = self.disambiguator(sch, mtab, route_col, wt0)
        warnings = [] if disamb else self.ambiguities(next(t for t in norm if t["name"] == mtab), route_col, wt0)
        for idx, j in enumerate(joins):                      # the meaning joins: city -> word city -> country -> …
            fw += f' JOIN {qident(j["right_table"])} ON {self._join_cond(idx, j, mtab, disamb, as_of)}'
        return fw, disamb, warnings

    def _join_cond(self, idx, j, mtab, disamb, as_of):
        """ON condition for one meaning join. idx==0 is the cell-side (uploaded value = world key)."""
        R = j["right_table"]
        cond = f'lower({qident(j["left_table"])}.{qident(j["left_col"])}) = lower({qident(R)}.{qident(j["right_col"])})'
        if idx == 0 and disamb:
            cond += f' AND lower({qident(mtab)}.{qident(disamb[0])}) = lower({qident(R)}.{qident(disamb[1])})'
        elif idx == 0 and "is_primary" in self.words[R].get("columns", []):
            cond += f' AND {qident(R)}.{qident("is_primary")} = 1'
        if "valid_from" in self.words[R]["columns"]:
            cond += (f' AND {qident(R)}.{qident("valid_from")} <= {qlit(as_of)}'
                     f' AND ({qident(R)}.{qident("valid_to")} IS NULL OR {qlit(as_of)} < {qident(R)}.{qident("valid_to")})')
        return cond

    def serve(self, tables, question, as_of=None, explicit_fks=()):
        as_of = as_of or datetime.date.today().isoformat()    # the DECISION time the answer is computed "as of"
        norm, fks = (
            self.q11.ingest(tables, explicit_fks=explicit_fks)
            if explicit_fks else self.q11.ingest(tables)
        )
        sch, colidx, tablemap = self.q11.schema(norm, fks)
        routes, coldims = {}, {}
        for t in norm:                                        # route cities + collect hover dims across ALL sheets
            routes.update(self.route(t)); coldims.update(self.column_dims(sch, t["name"]))
        agg = self.read_op_all(question, sch)                # (fn, table, col) | ("COUNT", table|None, None) | None
        world_rate = None                                     # knowledgebase-supplied conversion: only when NO uploaded
        conversion_early = self._currency_conversion_binding(question, agg, sch, fks)
        if conversion_early is None:                          # rate sheet binds (own data first)
            world_rate = self._world_rate_binding(question, agg, sch)
        q_for_mf = question
        if conversion_early or world_rate:
            # The conversion phrase is CLAIMED by the conversion: "in US dollars" must not also read
            # as the country United States. A country named OUTSIDE the phrase still filters.
            from engine.currency_intent import currency_intent as _ci
            intent = _ci(question)
            if intent is not None and getattr(intent, "phrase", None):
                q_for_mf = question.replace(intent.phrase, " ")
        self._q_meaning = q_for_mf                            # the entities layer resolves values from this
        mf = self.meaning_filter(q_for_mf, routes)
        own = self._own_value_matches(question, norm)         # values quoted in the question that live in the upload
        if mf is not None and any(mf["value"].lower() in v.lower() for _, _, v in own):
            mf = None                                         # the value (or a longer own value CONTAINING it — world
                                                              # "United States" vs uploaded "United States of America")
                                                              # lives in the upload -> the user's data answers it
        wtarget = self.world_target(question, routes)        # a WORLD column to SELECT/AGGREGATE (population, currency, …)
        # interrogative projection: "which/what <world col> is <entity> in/of" PROJECTS that column for the named
        # entity — the column word is NOT a filter value (meaning_filter may spuriously self-match it) and NOT a COUNT.
        # Drop that self-matched filter so the projection survives the col==filter null below; the spurious COUNT is
        # suppressed after read_op_all. ("how many <col>" stays COUNT(DISTINCT) — not matched by this which/what cue.)
        proj_world_col = bool(wtarget and re.search(r"\b(which|what)\s+" + re.escape(wtarget["word"]), question.lower()))
        if proj_world_col and mf and mf.get("attr") == wtarget["col"]:
            mf = None
        if wtarget and mf and wtarget["col"] == mf["attr"]:  # if that column is the FILTER itself ("in the northern
            wtarget = None                                   # hemisphere"), it's the predicate, not the projection
        if wtarget and any(wmatch(wtarget["word"], w) or wtarget["word"] == w
                           for c in sch for w in name_words(c["name"])):
            wtarget = None                                   # the upload has its OWN column of that name ("unique
                                                             # countries" + a Country column) -> own data wins
        if mf is None and wtarget is None and world_rate is None:   # neither a world filter NOR a world column NOR a
            r = (self.q11.serve(tables, question, explicit_fks=explicit_fks)
                 if explicit_fks else self.q11.serve(tables, question))
            response = {"question": question, "as_of": as_of, "sql": r.get("sql"), "result": r.get("result"),
                    "error": r.get("error"), "routed": {f"{t}.{c}": wt for (t, c), wt in routes.items()},
                    "dims": coldims, "meaning_join": None, "provenance": None, "warnings": [],
                    "planner": {
                        "ast": r.get("ast"),
                        "candidate_count": r.get("candidate_count"),
                        "evidence": r.get("evidence", []), "features": r.get("features", {}),
                    },
                    "debug": self._debug_input(norm, question, [],   # own-data path: no world table, no meaning plan
                        [{"col": f'{t["name"]}.{c}', "dims": coldims.get(c)} for t in norm for c in t["columns"] if coldims.get(c)], [], True),
                    "model": r.get("model", "engine - own-data planner (own-data SQL; no world-knowledge join)")}
            if r.get("calculations") is not None:
                response["calculations"] = r["calculations"]
            if r.get("currency") is not None:  # compatibility projection of calculations
                response["currency"] = r["currency"]
            return response
        # ---- WORLD-KNOWLEDGE path — uploaded FK joins + the meaning joins, WHERE from the world filter (if any)
        # AND any own-sheet values the question also quoted ("GOLD customers in France") ----
        mtab = (mf["csv_table"] if mf else wtarget["path"][0]["left_table"] if wtarget
                else world_rate["fact"])                     # conversion-only: the fact sheet drives the join
        route_col = (mf["csv_col"] if mf else wtarget["path"][0]["left_col"] if wtarget
                     else world_rate["ccy_col"])
        if proj_world_col and agg and agg[0] == "COUNT":     # an interrogative projection is DISTINCT, not a COUNT
            agg = None                                       # (the permissive count gate fires on 'which continent…')
        namecol = next((c["name"] for c in sch if c["table"] == mtab and c["affinity"] == "TEXT"), None)
        joins = list(mf["joins"]) if mf else []              # join chain = the FILTER joins UNION the world-target's path…
        if wtarget:                                          # …so the table holding the requested world column is joined in
            for j in wtarget["path"]:
                if j["right_table"] not in [x["right_table"] for x in joins]:
                    joins.append(j)
        own_filters = [(t, c, v) for (t, c, v) in own if not mf or v.lower() != mf["value"].lower()]
        conversion = self._currency_conversion_binding(question, agg, sch, fks)
        selected_measure = None
        selected_conversion = False
        query_tail = ""
        if wtarget and agg and agg[0] == "COUNT":            # "how many countries …" counts DISTINCT world values,
            proj = f'COUNT( DISTINCT {qident(wtarget["table"])}.{qident(wtarget["col"])} )'   # not join rows
            pdesc = ("aggregate", f'COUNT(DISTINCT {wtarget["table"]}.{wtarget["col"]})', "count cue + world column named")
            involved = [mtab]
        elif (wtarget and agg and agg[0] in ("SUM", "AVG")
              and (set(question.lower().split()) & (ARGMAX_CUES | ARGMIN_CUES))):
            # An ordinal request over a text dimension is a grouped aggregate, not a
            # DISTINCT projection.  Keep the aggregate in ORDER BY while returning
            # only the requested dimension, matching the natural answer shape for
            # "which continent has the highest total amount".
            measure = f'{qident(agg[1])}.{qident(agg[2])}'
            aggregate = self._numeric_aggregate(agg[0], measure)
            dimension = f'{qident(wtarget["table"])}.{qident(wtarget["col"])}'
            direction = "ASC" if set(question.lower().split()) & ARGMIN_CUES else "DESC"
            proj = dimension
            query_tail = f' GROUP BY {dimension} ORDER BY {aggregate} {direction} LIMIT 1'
            pdesc = ("select", f'{wtarget["table"]}.{wtarget["col"]} ({agg[0]} by dimension)',
                     f'ordered {direction.lower()} aggregate')
            involved = [mtab] + ([agg[1]] if agg[1] != mtab else [])
            selected_measure = (agg[1], agg[2])
        elif wtarget and agg and agg[0] in ("SUM", "AVG") and wtarget["affinity"] in ("INTEGER", "REAL"):
            operand = f'{qident(wtarget["table"])}.{qident(wtarget["col"])}'
            proj = self._numeric_aggregate(agg[0], operand)
            pdesc = ("aggregate", f'{agg[0]}({wtarget["table"]}.{wtarget["col"]})', "agg cue + world measure named")
            involved = [mtab] + ([agg[1]] if agg[1] != mtab else [])
            selected_measure = (wtarget["table"], wtarget["col"])
        elif wtarget:
            proj = f'DISTINCT {qident(wtarget["table"])}.{qident(wtarget["col"])}'      # SELECT DISTINCT the world attribute
            pdesc = ("select", f'DISTINCT {wtarget["table"]}.{wtarget["col"]}', "world column named")
            involved = [mtab]
        elif agg and agg[0] == "COUNT":
            proj = "COUNT( * )"
            pdesc = ("aggregate", "COUNT(*)", "count cue word")
            involved = [mtab, agg[1]] if (agg[1] and agg[1] != mtab) else [mtab]
        elif agg and world_rate:
            measure_sql = f'{qident(agg[1])}.{qident(agg[2])}'
            rate_sql = f'{qident("exchange_rate")}.{qident(world_rate["rate_col"])}'
            proj = self._numeric_aggregate("SUM", self._numeric_multiply(measure_sql, rate_sql))
            pdesc = (
                "aggregate",
                f'SUM({agg[1]}.{agg[2]} * exchange_rate.{world_rate["rate_col"]})',
                "explicit currency target + knowledgebase daily rate (code, date) join",
            )
            involved = list(dict.fromkeys((mtab, agg[1])))   # exchange_rate joins as a WORLD table, not a sheet
            selected_measure = (agg[1], agg[2])
            selected_conversion = True
        elif agg and conversion:
            rate_table, rate_col = conversion
            measure_sql = f'{qident(agg[1])}.{qident(agg[2])}'
            rate_sql = f'{qident(rate_table)}.{qident(rate_col)}'
            proj = self._numeric_aggregate("SUM", self._numeric_multiply(measure_sql, rate_sql))
            pdesc = (
                "aggregate",
                f'SUM({agg[1]}.{agg[2]} * {rate_table}.{rate_col})',
                "explicit currency target + typed direct-rate edge",
            )
            involved = list(dict.fromkeys((mtab, agg[1], rate_table)))
            selected_measure = (agg[1], agg[2])
            selected_conversion = True
        elif agg:
            operand = f'{qident(agg[1])}.{qident(agg[2])}'
            proj = self._numeric_aggregate(agg[0], operand)
            pdesc = ("aggregate", f'{agg[0]}({agg[1]}.{agg[2]})', "cue word + is_num feature")
            involved = [mtab, agg[1]] if (agg[1] and agg[1] != mtab) else [mtab]
            selected_measure = (agg[1], agg[2])
        elif (subjcol := self._subject_col(question, mtab, route_col, sch, routes)) or namecol:
            selcol = subjcol or namecol                          # routed subject column ("cities in US" -> City),
            proj = f'{qident(mtab)}.{qident(selcol)}'            # else the first text column (unchanged default)
            pdesc = ("select", f'{mtab}.{selcol}',
                     "routed subject column" if subjcol else "first text column")
            involved = [mtab]
        else:
            proj = "*"
            pdesc = ("select", "*", "no projection named")
            involved = [mtab]
        involved += [t for (t, c, v) in own_filters if t not in involved]   # own filters pull their sheet into the FROM
        upfrom, updescs, joined, selected_fks = self._uploaded_from(
            involved, fks,
        )  # FROM <measure sheet> JOIN <city sheet> ON <uploaded FK>
        unjoined = [t for t in involved if t not in joined]
        if unjoined:                                          # a clean error beats SQLite's "no such column"
            return {"question": question, "as_of": as_of, "sql": None, "result": None,
                    "error": f"no foreign key relates sheet '{unjoined[0]}' to '{involved[0]}' — cannot combine them",
                    "routed": {f"{t}.{c}": wt for (t, c), wt in routes.items()}, "dims": coldims,
                    "meaning_join": None, "provenance": None, "warnings": [],
                    "debug": self._debug_input(norm, question, [], [], [], False),
                    "model": "engine - CSV -> world meaning join + bitemporal/freshness"}
        if joins:
            fw, disamb, warnings = self._world_joins(upfrom, joins, sch, norm, mtab, route_col, as_of, mf)
        else:
            fw, disamb, warnings = upfrom, None, []          # conversion-only: no meaning joins to walk
        fw_no_rate = fw
        fw_join = fw                                         # joins walked, no rate join, no filters — the 'joined' step
        if world_rate:
            # The knowledgebase table joins like any other table in the conversation: by VALUE on the
            # (code, date) pair. The date compares as ISO text on both engines (Postgres date::text is
            # ISO; SQLite stores dates as text) — non-ISO upload dates simply fail to join and are
            # caught by the row-coverage check below, which declines rather than dropping rows.
            date_side = (f'{qident(world_rate["fact"])}.{qident(world_rate["date_col"])}'
                         if world_rate["date_col"] else qlit(as_of))
            fw += (f' JOIN {qident("exchange_rate")} ON '
                   f'lower({qident(world_rate["fact"])}.{qident(world_rate["ccy_col"])}) = '
                   f'lower({qident("exchange_rate")}.{qident("currency_code")})'
                   f' AND CAST({qident("exchange_rate")}.{qident("date")} AS text) = {date_side}')
        conds = ([f'{qident(mf["filter_table"])}.{qident(mf["attr"])} = {qlit(mf["value"])}'] if mf else [])
        conds += [f'lower({qident(t)}.{qident(c)}) = lower({qlit(v)})' for (t, c, v) in own_filters]
        if conds:
            where_clause = " WHERE " + " AND ".join(conds)
            fw += where_clause
            fw_no_rate += where_clause
        mdesc = " ; ".join(f'{j["left_table"]}.{j["left_col"]} = {j["right_table"]}.{j["right_col"]}' for j in joins)
        join_desc = ("; ".join(updescs) + " ; " if updescs else "") + mdesc
        ft = mf["filter_table"] if mf else wtarget["table"] if wtarget else "exchange_rate"
        prov = {"table": ft, "attr": (mf["attr"] if mf else wtarget["col"] if wtarget
                                      else world_rate["rate_col"]),
                "value": (mf["value"] if mf else None), "hops": len(joins),
                "disambiguated_by": (f'{mtab}.{disamb[0]}' if disamb else None),
                "source": self.words[ft].get("source"), "as_of": as_of}
        sql = f'SELECT {proj} {fw}{query_tail}'

        ok, why = self.q11.guard(sql)
        result, err = None, None
        coverage_gap = None
        response_views = None
        if ok:
            try:
                con = self._connect(tablemap, sch, attach_world=bool(joins) or bool(world_rate))
                cur = con.execute(sql)
                cols = [d[0] for d in cur.description]
                result = {"columns": cols, "rows": wire_rows(cur.fetchall()[:50])}
                if proj_world_col and result["rows"] and hasattr(self, "_labelize_qids"):
                    self._labelize_qids(result)   # a projected world entity-attr column holds QIDs -> show 'Asia', not 'Q48'
                if mf:                            # FRESHNESS GUARD — trace the word rows that ACTUALLY contributed
                    ft = mf["filter_table"]; key = self.words[ft]["key"]
                    if "updated_at" in self.words[ft]["columns"]:
                        used = con.execute(f'SELECT DISTINCT {qident(ft)}.{qident(key)}, {qident(ft)}.{qident("updated_at")} {fw}').fetchall()
                        stale = [(nm, uv) for nm, uv in used if uv and self._days(uv, as_of) > STALE_DAYS]
                        if stale:
                            nm, uv = max(stale, key=lambda x: self._days(x[1], as_of))
                            warnings.append(f"freshness: '{nm}' ({ft}) was last verified {uv}, {self._days(uv, as_of)} "
                                            f"days before the as-of date {as_of} — may be stale")
                    else:
                        # No per-row updated_at: judge the TABLE from the maintenance catalog instead of
                        # passing silently. An overdue table (past its declared cadence) and one nobody
                        # maintains are different answers, and both are worth saying out loud.
                        warnings.extend(self._table_freshness(con, ft, as_of))
                if world_rate and result is not None:
                    release_row = con.execute(
                        f'SELECT source_release_id FROM {qident("exchange_rate")} LIMIT 1'
                    ).fetchone()
                    prov["release_id"] = release_row[0] if release_row else None
                    base_n = con.execute(f'SELECT COUNT(*) {fw_no_rate}').fetchone()[0]
                    conv_n = con.execute(f'SELECT COUNT(*) {fw}').fetchone()[0]
                    coverage_gap = (base_n - conv_n, base_n) if conv_n < base_n else None
                    if coverage_gap is None:
                        fact_q, er_q = qident(world_rate["fact"]), qident("exchange_rate")

                        def _wire_rows(c):
                            # Postgres returns date objects; every view crosses TWO JSON boundaries
                            # (the RTDB stream and the HTTP envelope), so serialize at the source —
                            # in-process tests see exactly what the wire carries.
                            return [["" if v is None
                                     else v.isoformat() if isinstance(v, (datetime.date, datetime.datetime))
                                     else v for v in row] for row in c.fetchall()]

                        # The full derivation trail, one sheet per step (the compose stack's
                        # join -> filter -> aggregate design): the resolution slides stream
                        # separately from the serving host (knowledge_compose).
                        response_views = []
                        if updescs or joins:                 # a real combine happened -> the joined rows
                            joined_sql = f'SELECT {fact_q}.* {fw_join} LIMIT 50'
                            jc = con.execute(joined_sql)
                            response_views.append({
                                "name": "joined", "op": "join",
                                "label": "join " + " + ".join(joined),
                                "sql": joined_sql, "columns": [d[0] for d in jc.description],
                                "rows": _wire_rows(jc),
                            })
                        if conds:                            # the world/own filter -> the kept rows
                            filtered_sql = f'SELECT {fact_q}.* {fw_no_rate} LIMIT 50'
                            fc = con.execute(filtered_sql)
                            flabel = (f'where {mf["attr"]} = {mf["value"]!r}' if mf else
                                      "where " + " and ".join(f"{c} = {v!r}" for (_t, c, v) in own_filters))
                            response_views.append({
                                "name": "filtered", "op": "world_filter" if mf else "filter",
                                "label": flabel, "sql": filtered_sql,
                                "columns": [d[0] for d in fc.description],
                                "rows": _wire_rows(fc),
                            })
                        # The CALCULATED view: the one non-obvious arithmetic, made a visible
                        # per-row column — each amount beside the exact rate it was multiplied by
                        # and that rate's true publication date, so Result is this column summed.
                        product_sql = self._numeric_multiply(
                            f'{fact_q}.{qident(agg[2])}',
                            f'{er_q}.{qident(world_rate["rate_col"])}',
                        )
                        calc_sql = (
                            f'SELECT {fact_q}.*, {er_q}.{qident(world_rate["rate_col"])}, '
                            f'{er_q}.{qident("updated_at")} AS rate_published, '
                            f'{product_sql} '
                            f'AS converted {fw} LIMIT 50'
                        )
                        calc_cur = con.execute(calc_sql)
                        response_views.append({
                            "name": "calculated", "op": "convert", "label": "calculated",
                            "sql": calc_sql,
                            "columns": [d[0] for d in calc_cur.description],
                            "rows": _wire_rows(calc_cur),
                        })
                        # The UI overlays the final Result onto the LAST view, so the total must be
                        # its own view — otherwise the per-row calculated grid is replaced by the SUM.
                        response_views.append({
                            "name": "total", "op": "group_agg", "label": "total", "sql": sql,
                            "columns": [d[0] for d in cur.description],
                            "rows": result["rows"],
                        })
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
        else:
            err = "guard: " + why
        mt = next(t for t in norm if t["name"] == mtab)        # the uploaded city values seed the participating world rows
        sci = mt["columns"].index(route_col) if route_col in mt["columns"] else 0
        seed = list({str(r[sci]) for r in mt["rows"] if r[sci] not in (None, "")})
        world_sections, world_dims = self.world_encode(joins, seed)   # GENUINELY encode the world rows + read their dims
        coldims.update(world_dims)                             # world tables' named dims -> visible on the SQL tokens
        feats = [{"col": f'{t["name"]}.{c}', "dims": coldims.get(c)} for t in norm for c in t["columns"] if coldims.get(c)]
        feats += [{"col": k, "dims": v} for k, v in world_dims.items() if "." in k]   # + the world columns' read dims
        plan = self._build_plan(pdesc, updescs, joins, mf, own_filters)
        response = {"question": question, "as_of": as_of, "sql": sql, "result": result, "error": err,
                "views": response_views or [],
                "routed": {f"{t}.{c}": wt for (t, c), wt in routes.items()}, "dims": coldims,
                "meaning_join": join_desc, "provenance": prov, "warnings": warnings,
                "debug": self._debug_input(norm, question, world_sections, feats, plan, False),
                "model": "engine - CSV -> world meaning join + bitemporal/freshness"}
        from engine.calculations import (
            BranchEvidence, ComputationEvidence, JoinFact, OutputEvidence, PredicateFact,
            assess_calculations,
        )
        from engine.calculations.core import aggregate_functions, expression_columns
        from engine.calculations.registry import attach_calculation_evidence
        from engine.sql_ast import Aggregate, BinaryExpr, ColumnRef, SQLType
        from engine.sql_schema import SchemaGraph
        predicates = frozenset(
            PredicateFact(table, column, "=", value)
            for table, column, value in own_filters
        )
        if world_rate:
            # The graph mirrors how the SQL uses the table: the date column appears only when the fact
            # is dated (composite join). For an undated fact the as_of pin is a constant predicate, so
            # the verifier sees the same plain (code -> rate) shape an uploaded rate sheet has.
            sch = list(sch) + [
                {"table": "exchange_rate", "name": "currency_code", "affinity": "TEXT", "values": []},
                {"table": "exchange_rate", "name": world_rate["rate_col"], "affinity": "REAL", "values": []},
            ] + ([{"table": "exchange_rate", "name": "date", "affinity": "TEXT", "is_date": True,
                   "values": []}] if world_rate["date_col"] else [])
            pair_from = [world_rate["ccy_col"]] + ([world_rate["date_col"]] if world_rate["date_col"] else [])
            pair_to = ["currency_code"] + (["date"] if world_rate["date_col"] else [])
            world_fk = {"from_table": world_rate["fact"], "to_table": "exchange_rate",
                        "from_col": world_rate["ccy_col"], "to_col": "currency_code",
                        "from_cols": pair_from, "to_cols": pair_to, "conf": 1.0}
            fks = list(fks) + [world_fk]
            selected_fks = list(selected_fks) + [world_fk]
        graph = SchemaGraph.from_planner(sch, fks)

        def _typed(table, column):
            """The column as the SCHEMA declares it.

            ColumnRef is a frozen dataclass, so its equality covers the declared SQL type. Synthesizing
            the measure as REAL made an integer-typed column (the demo's whole-currency amounts) compare
            unequal to the planned expression, so a correct SUM(amount * rate_to_usd) was reported as
            not converting and the world path declined a right answer.
            """
            entry = graph.column_map.get((table, column))
            return entry.ref if entry is not None else ColumnRef(table, column, SQLType.REAL)

        outputs = ()
        if selected_measure:
            measure = _typed(*selected_measure)
            expression = Aggregate(agg[0], measure)
            rate_binding = conversion or (world_rate and ("exchange_rate", world_rate["rate_col"]))
            if selected_conversion and rate_binding:
                expression = Aggregate("SUM", BinaryExpr(measure, "*", _typed(*rate_binding)))
            outputs = (OutputEvidence(
                expression, True, aggregate_functions(expression), expression_columns(expression),
            ),)
        join_facts = []
        for foreign_key in selected_fks:
            from_cols = tuple(foreign_key.get("from_cols") or (foreign_key["from_col"],))
            to_cols = tuple(foreign_key.get("to_cols") or (foreign_key["to_col"],))
            pairs = tuple(
                (
                    graph.column_map[(foreign_key["from_table"], left)].ref,
                    graph.column_map[(foreign_key["to_table"], right)].ref,
                )
                for left, right in zip(from_cols, to_cols)
            )
            join_facts.append(JoinFact(pairs))
        computation = ComputationEvidence((BranchEvidence(outputs, predicates, tuple(join_facts)),))
        assessments = assess_calculations(
            question, norm, graph, computation,
        )
        if world_rate and coverage_gap:
            gap, base = coverage_gap
            assessments = tuple(list(assessments) + [{
                "specification": "currency", "status": "unmet", "realization": None,
                "reason": (f"{gap} of {base} rows have no ECB reference rate for their "
                           f"(currency, date) — outside published coverage"),
                "proposal": "",
            }])
        attach_calculation_evidence(response, assessments)
        return response

    def _table_freshness(self, con, table, as_of):
        """Warnings from the maintenance catalog for a table with no per-row updated_at. The offline
        SQLite path has no catalog to consult, so it claims nothing; PgQuery overrides this."""
        return []

    def _connect(self, tablemap, sch, attach_world):
        """in-memory SQLite with the uploaded sheet(s); ATTACH words.db whenever the SQL joins ANY world table
        (a filter join OR a world-column path — not only when a filter exists)."""
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
            t = tablemap[tname]
            ins = f"INSERT INTO {qident(tname)} VALUES ({','.join('?' * len(cols))})"
            for r in t["rows"]:
                rd = dict(zip(t["columns"], r))
                con.execute(ins, [sqlite_numeric(rd.get(c["name"]), c["affinity"])
                                  if c["affinity"] in ("INTEGER", "REAL") else str(rd.get(c["name"]))
                                  if rd.get(c["name"]) is not None else None for c in cols])
        if attach_world:                          # ATTACH the persistent meaning DB (no per-query bulk insert —
            con.execute("ATTACH DATABASE ? AS words", (str(self.dbpath),))   # joins hit the indexed word tables)
        return con

    @staticmethod
    def _days(a, b):
        """days from a to b (positive when a is BEFORE b, i.e. record `a` is older than the as-of `b`)."""
        try:
            return (datetime.date.fromisoformat(str(b)[:10]) - datetime.date.fromisoformat(str(a)[:10])).days
        except Exception:
            return 0

    @staticmethod
    def _coerce(v, aff):
        if v is None:
            return None
        if aff in ("INTEGER", "REAL"):
            return coerce_numeric(v, aff)
        return str(v)
