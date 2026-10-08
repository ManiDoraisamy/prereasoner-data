"""The COMPOSITIONAL view-stacking reasoner.

A complex analytical question is DECOMPOSED into a DAG of simple analytical primitives, each materialized as a VIEW
stacked over the previous — so complexity comes from DEPTH (composition), not from one complex query the single-
template engine can't emit:

  "top 3 products by YoY growth excluding returns"  ->
     v1 = filter(exclude returns) | v2 = SUM(amount) by product,year | v3 = YoY self-join | v4 = top 3 by yoy

The EXECUTION architecture: primitives compose into correct complex answers a single template can't reach, and
the decomposition REPRESENTATION is adequate (real questions -> short primitive DAGs). It runs on SQLite for
execution. The operand binding — operator (SUM/COUNT/AVG) + measure column — reads off the UNIFIED ENCODER when
one is supplied (`read_op_model` for the operator, cosine in the contrastive space for the measure), matching
the live world engine (EncoderQuery.read_op_all): NO measure-noun list, NO operator keyword map on that path.
The keyword heuristics survive only as the encoder-FREE fallback (so this stays testable without a model). The
composition PATTERNS (yoy / topn / exclusion) are read off the LEARNED primitive head (PrimitiveReader); each
remaining heuristic is a clearly-marked encoder-free seam.
"""
from __future__ import annotations
import re
import sqlite3
from decimal import Decimal

import numpy as np

from engine.primitives import (q, filter_view, group_agg_view, yoy_view, topn_view, share_view,
                               divide_view, running_view, join_view, world_join_view)
from engine.closed_class import EXCLUSION_CUES, counted_rows, measured_rows
from engine.joins import discover_fks, join_plan
from engine.numeric import GROUPED_DIGITS, parse_decimal, register_sqlite_decimal, sqlite_numeric, wire_decimal
from engine.query_contract import unreadable_cells, unreadable_measure_reason
from engine.sql_schema import is_surrogate_key

MEASURE_WORDS = {"amount", "revenue", "sales", "spend", "cost", "price", "value", "quantity", "qty", "margin",
                 "profit", "income", "turnover"}   # encoder-FREE FALLBACK ONLY — with an encoder the measure is cosine


class UnreadableMeasure(ValueError):
    """The measure the question names has cells that are not numbers; its message is the reply."""


class ComposeEngine:
    """Decompose a question into a primitive plan, then materialize it as a stack of SQLite views. With a
    PrimitiveReader the STRUCTURE (which primitives) is read off the LEARNED primitive head; with the unified encoder
    the OPERANDS (operator + measure column) are read off the metric space (read_op_model + cosine, like the live
    world engine). Without an encoder both fall back to transparent regex/keyword heuristics."""

    EXCL_TRIGGER = EXCLUSION_CUES                           # heuristic flag only; shared with the semantic search

    def __init__(self, reader=None, encoder=None):
        self.reader = reader                                # PrimitiveReader | None — the learned primitive readout
        # the unified encoder (EncoderQuery): operator from read_op_model + measure by cosine, matching the live
        # world engine. Reuse the reader's encoder so it loads ONCE; None => encoder-free keyword/regex fallback.
        self.enc = encoder if encoder is not None else (reader.enc if reader is not None else None)

    # ---------------- column typing ----------------
    @staticmethod
    def _isnum(v):
        try:
            parse_decimal(v); return True
        except (ValueError, TypeError):
            return False

    def _types(self, cols, rows):
        """num / time / text per column (time = a year column — the axis YoY needs)."""
        typ = {}
        for ci, c in enumerate(cols):
            vals = [r[ci] for r in rows if r[ci] is not None and str(r[ci]).strip()]
            nm = c.lower()
            if nm == "year" or nm.endswith("_year") or (vals and all(re.fullmatch(r'(19|20)\d\d', str(v).strip()) for v in vals)):
                typ[c] = "time"
            elif vals and all(self._isnum(v) for v in vals):
                typ[c] = "num"
            else:
                typ[c] = "text"
        return typ

    @staticmethod
    def _mentions(c, low):
        n = c.lower()
        plu = (n[:-1] + "ies") if n.endswith("y") else (n + "s")            # singular column -> plural in the question
        return n in low or plu in low or (len(n) >= 5 and n[:len(n) - 2] in low)   # name / plural / loose stem

    @staticmethod
    def _names_attribute(c, low):
        """STRICT whole-word naming — the necessity test. _mentions' loose stem is evidence for BUILDING a
        join, but 'country'[:5] == 'count' made the verb in 'Count all rows...' read as naming the country
        attribute; necessity demands the question actually say the word (or its plural)."""
        n = str(c).lower()
        toks = set(re.findall(r"[a-z]+", low))
        plu = (n[:-1] + "ies") if n.endswith("y") else (n + "s")
        return n in toks or plu in toks

    # ---------------- decomposition (each detector is a seam for an anchored PRIMITIVE dim) ----------------
    def _pick_measure(self, low, numeric, question=None):
        """The measure column. Explicit name match (data-driven) > COSINE in the unified space (the encoder path —
        'how much did we earn' lands on `earnings`, retiring the MEASURE_WORDS list) > encoder-free keyword fallback.
        `numeric` is already non-id (the only structural rule the live engine keeps for operands)."""
        if not numeric:
            return None
        toks = set(re.findall(r'[a-z]+', low))
        for c in numeric:                                   # column named explicitly (data-driven, not a noun list)
            if c.lower() in toks:
                return c
        if self.enc is not None and question is not None:   # COSINE among non-id numerics (like read_op_all)
            if len(numeric) == 1:
                return numeric[0]
            V = self.enc._encode([question, *numeric])
            qv = V[0]
            sims = [float(qv @ V[1 + i] / ((np.linalg.norm(qv) * np.linalg.norm(V[1 + i])) + 1e-9))
                    for i in range(len(numeric))]
            return numeric[int(np.argmax(sims))]
        named = [c for c in numeric if c.lower() in MEASURE_WORDS]   # encoder-free fallback (hardcoded noun list)
        return named[0] if named else numeric[0]

    def _intent(self, low, table=None, question=None):
        """The aggregate the question asks for, or None when it asks for none ('which cities have ...'). FROM THE
        MODEL (read_op_model reads intent_agg_sum/count/avg off the question's verb — 'how much'->SUM with no
        keyword cue) when an encoder is present; else the keyword map."""
        if self.enc is not None and table is not None and question is not None:
            op, _ = self.enc.read_op_model([table], question)
            return op
        if re.search(r'\baverage\b|\bmean\b|\bavg\b', low):
            return "AVG"
        if re.search(r'how many|number of|\bcount\b', low):
            return "COUNT"
        if re.search(r'\btotal\b|\bsum\b', low):
            return "SUM"
        return None

    def _pick_dim(self, low, texts, question=None):
        """The grouping / entity dimension when none is named explicitly. Explicit mention (data-driven) > COSINE in
        the unified space (the encoder path — 'top sellers by revenue' lands on a `seller`/`name` column) > the first
        text column (encoder-free fallback). This retires the hardcoded `texts[0]` first-column default."""
        if not texts:
            return None
        for c in texts:                                     # column named explicitly (incl. plural)
            if self._mentions(c, low):
                return c
        if self.enc is not None and question is not None and len(texts) > 1:
            V = self.enc._encode([question, *texts]); qv = V[0]
            sims = [float(qv @ V[1 + i] / ((np.linalg.norm(qv) * np.linalg.norm(V[1 + i])) + 1e-9))
                    for i in range(len(texts))]
            return texts[int(np.argmax(sims))]
        return texts[0]

    def _excluded_value(self, low, texts, cols, rows):
        """The (column, value) to exclude — found by matching a question word to a distinct categorical VALUE, so it
        is INDEPENDENT of the trigger phrase ('net of returns' resolves to status='returned' even though the regex
        flag wouldn't catch 'net of'). Gated by the EXCL primitive being present."""
        toks = [w for w in re.findall(r'[a-z]+', low) if len(w) > 2]
        for c in texts:
            ci = cols.index(c)
            vals = {str(r[ci]) for r in rows if r[ci] is not None}
            for w in toks:
                stem = w.rstrip('s')
                for v in vals:
                    if stem and stem in v.lower():
                        return (c, v)
        return None

    # Query words that are NEVER a filter value even if they happen to sit in a text cell (a summary 'Total' row, a
    # 'name' header echoed as data) — they'd otherwise let 'total amount' self-match a cell and filter on itself.
    _VALUE_STOP = frozenset({"total", "amount", "sum", "count", "average", "avg", "mean", "number", "all", "the",
                             "and", "for", "per", "each", "top", "row", "rows", "value", "values", "name", "names",
                             "data", "table", "none", "null", "true", "false", "yes", "no"})

    def _value_filter(self, low, texts, cols, rows, skip=None):
        """The (column, value) to KEEP: a question phrase that EXACTLY names a distinct categorical VALUE already in a
        text column ('total amount in Chennai' -> city = 'Chennai'). This is the plain input-column filter that needs
        NO world model — the value is right there in the data, so we filter on it directly (the world join/attribute
        path only handles values that are NOT in the upload, e.g. a country the cities roll up to). Whole-value word-
        boundary match (so multi-word 'New York' works and 'in' never matches); returns the LONGEST match (most
        specific), or None. `skip` is a value already claimed elsewhere (e.g. by exclusion)."""
        best = None                                          # (col, value, match_len)
        for c in texts:
            ci = cols.index(c)
            for v in {str(r[ci]) for r in rows if ci < len(r) and r[ci] not in (None, "")}:
                vl = v.lower()
                if len(vl) < 3 or v == skip or vl in self._VALUE_STOP or vl not in low:
                    continue                                 # only a value inside the question can match below
                if re.search(r'\b' + re.escape(vl) + r'\b', low) and (best is None or len(vl) > best[2]):
                    best = (c, v, len(vl))
        return (best[0], best[1]) if best else None

    def _dims(self, low, texts, table_names=(), row_nouns=()):
        """Text columns the question mentions: each one a grouping candidate. The rows being aggregated are named
        by an uploaded table's own name or by the noun the question measures or counts ('the total amount of GBP
        orders'), and either is claimed before the loose stem match: 'total amount of GBP orders in Europe'
        grouped the `ordered` column because 'ordered'[:5] == 'order' (2026-09-30), and again on any sheet not
        named orders (Sheet1, 2026-10-08). A column the question names outright ('by product' over a 'products'
        table, 'by ordered item') still groups."""
        claimed = low
        for table_name in (*table_names, *row_nouns):
            name = " ".join(re.findall(r"[a-z0-9]+", str(table_name).lower()))
            if not name:
                continue
            singular = name[:-1] if name.endswith("s") else name
            for form in sorted({name, singular, singular + "s"}, key=len, reverse=True):
                claimed = re.sub(r"(?<![a-z0-9])" + re.escape(form) + r"(?![a-z0-9])", " ", claimed)
        return [c for c in texts if self._names_attribute(c, low) or self._mentions(c, claimed)]

    def _topn(self, low):
        m = re.search(r'(?:top|highest|best|largest|biggest)\s+(\d+)', low)
        if m:
            return (int(m.group(1)), True)
        m = re.search(r'(?:bottom|lowest|worst|smallest)\s+(\d+)', low)
        if m:
            return (int(m.group(1)), False)
        if re.search(r'\btop\b|\bhighest\b|\bbest\b|\blargest\b', low):
            return (3, True)
        if re.search(r'\bbottom\b|\blowest\b|\bworst\b', low):
            return (3, False)
        return None

    def _sort(self, low):                                   # order WITHOUT a limit (vs _topn which has top/best/N)
        if re.search(r'sorted by|ranked by|ordered by|order by|in (?:descending|ascending) order|'
                     r'from (?:highest|lowest|largest|smallest)', low):
            return not bool(re.search(r'ascending|from lowest|from smallest|lowest first', low))   # True = DESC
        return None

    def _time_pred(self, low, times):
        """A temporal predicate read off explicit year tokens: 'in 2023' -> =, 'since/after 2020' -> >=, 'before
        2022' -> <, 'between 2020 and 2023' -> range. Returns [(col, op, value)] on the time column, or []."""
        if not times:
            return []
        tcol = times[0]
        yrs = [int(y) for y in re.findall(r'\b((?:19|20)\d\d)\b', low)]
        if not yrs:
            return []
        m = re.search(r'between\s+((?:19|20)\d\d)\s+and\s+((?:19|20)\d\d)', low)
        if m:
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            return [(tcol, ">=", a), (tcol, "<=", b)]
        if re.search(r'\b(since|after|from)\b', low):
            return [(tcol, ">=", min(yrs))]
        if re.search(r'\bbefore\b', low):
            return [(tcol, "<", min(yrs))]
        if len(yrs) == 1:
            return [(tcol, "=", yrs[0])]
        return [(tcol, ">=", min(yrs)), (tcol, "<=", max(yrs))]

    # A scale word after a comparison's number ('over 1 million').
    _SCALE = {"thousand": 10 ** 3, "million": 10 ** 6, "billion": 10 ** 9, "trillion": 10 ** 12}
    # Words between an attribute and its comparison that name nothing ('a population of over 1 million', 'whose
    # mass is above 10').
    _COMPARED_FILLER = frozenset({"a", "an", "the", "of", "is", "are", "was", "were", "be", "been", "being"})
    # Words that join a noun to the clause qualifying it ('cities with / whose / that have a population ...').
    _CLAUSE_LINK = frozenset({"with", "whose", "where", "having", "that", "which", "who", "have", "has", "had",
                              "a", "an", "the", "their", "its"})
    # An aggregate word before the compared attribute makes the comparison one on that aggregate ('customers
    # with total amount over 100' thresholds each customer's total).
    _AGGREGATE_WORDS = frozenset({"total", "sum", "average", "avg", "mean"})
    # Words that make the noun after them the set an aggregate runs over ('the average population OF these
    # cities', 'total amount FOR cities in France'), and the determiners that may stand in between.
    _DOMAIN_LINK = frozenset({"of", "in", "for", "from", "among", "across"})
    _DETERMINERS = frozenset({"the", "these", "those", "this", "that", "all", "both", "our", "my", "your", "its",
                              "their", "any", "some"})
    # Words that make the noun after them a grouping ('by city', 'per city', 'each city', 'each of the cities'),
    # and words that rank it ('the top 3 cities', 'the biggest cities'): either keeps a mention a grouping.
    _GROUPING_CUES = frozenset({"by", "per", "each", "every"})
    _RANKING_WORDS = frozenset({"top", "bottom", "highest", "lowest", "largest", "smallest", "biggest", "best",
                                "worst", "most", "least", "first", "last"})

    @staticmethod
    def _forms(words):
        """A name's words as a question says them: as named, or with the last word plural ('city', 'cities')."""
        last = words[-1]
        return {words, words[:-1] + ((last[:-1] + "ies") if last.endswith("y") else (last + "s"),)}

    def _threshold(self, low, cols):
        """A threshold and the attribute it compares. 'over/more than 1000' -> ('>', 1000), 'at least 5' ->
        ('>=', 5), 'under/less than 100' -> ('<', 100), 'at most 3' -> ('<=', 3); a scale word multiplies ('over
        1 million' -> 1000000). The comparison binds the column of `cols` named right before it ('cities with
        population over 1,000,000', 'whose mass is above 10') or right after its value ('more than 1 million
        population'). -> (column | None, op, value, (start, end), aggregated) or None, where (start, end) spans
        the named column and the comparison in `low`, column is None when the comparison names no column, and
        aggregated says an aggregate word precedes the column ('total amount over 100')."""
        for pattern, strict, inclusive in (
                (r"over|above|more than|greater than|exceed(?:s|ing)?|at least", ">", "at least"),
                (r"under|below|less than|fewer than|at most", "<", "at most")):
            # The value is a number as numeric.py reads one: "over 1,000, by city" compares 1,000, and
            # "over 1,5" names no number to compare.
            m = re.search(r"\b(" + pattern + r")\s*\$?((?:" + GROUPED_DIGITS + r"|\d+)(?:\.\d+)?)(?!,?\d)"
                          r"(?:\s+(thousand|million|billion|trillion)\b)?", low)
            if m:
                break
        else:
            return None
        op = strict + "=" if m.group(1) == inclusive else strict
        value = parse_decimal(m.group(2)) * self._SCALE.get(m.group(3), 1)
        if value == value.to_integral_value():
            value = Decimal(int(value))
        before = list(re.finditer(r"[a-z0-9]+", low[:m.start()]))
        while before and before[-1].group() in self._COMPARED_FILLER:
            before.pop()
        after = list(re.finditer(r"[a-z0-9]+", low[m.end():]))
        best = None                                          # ((name length, named before), column, span)
        for c in cols:
            name = tuple(re.findall(r"[a-z0-9]+", str(c).lower()))
            n = len(name)
            if not n:
                continue
            if len(before) >= n and tuple(w.group() for w in before[-n:]) in self._forms(name):
                key, span = (n, 1), (before[-n].start(), m.end())
            elif len(after) >= n and tuple(w.group() for w in after[:n]) in self._forms(name):
                key, span = (n, 0), (m.start(), m.end() + after[n - 1].end())
            else:
                continue
            if best is None or key > best[0]:
                best = (key, c, span)
        if best is None:
            return None, op, value, (m.start(), m.end()), False
        (n, named_before), column, span = best
        lead = [w.group() for w in before[:-n]] if named_before else []
        while lead and lead[-1] in self._COMPARED_FILLER:
            lead.pop()
        return column, op, value, span, bool(lead) and lead[-1] in self._AGGREGATE_WORDS

    def _aggregated_over(self, c, low, clause_start=None):
        """Is text column `c` named only as the set an aggregate runs over, never as a grouping? Each mention must
        be the object of a domain word, with determiners and at most one other word in between ('the average
        population of these cities', 'total amount for cities in France', 'in big cities'), or the noun a row
        threshold qualifies ('big cities with population over 1,000,000', the clause starting at `clause_start`).
        A grouping cue ('by city', 'per city', 'each city', 'for each of the cities'), a ranking word or a number
        ('the top 3 cities') keeps a mention a grouping, and so does any other position ('which cities', 'top
        cities', 'city totals')."""
        name = tuple(re.findall(r"[a-z0-9]+", str(c).lower()))
        if not name:
            return False
        tokens = [(m.group(), m.start()) for m in re.finditer(r"[a-z0-9]+", low)]
        words = [word for word, _ in tokens]
        n, forms = len(name), self._forms(name)
        mentions = [i for i in range(len(words) - n + 1) if tuple(words[i:i + n]) in forms]
        if not mentions:
            return False
        for i in mentions:
            if i and words[i - 1] in self._GROUPING_CUES:
                return False
            qualified = clause_start is not None and tokens[i + n - 1][1] < clause_start and all(
                word in self._CLAUSE_LINK for word, start in tokens[i + n:] if start < clause_start)
            if not (qualified or self._domain_object(words, i)):
                return False
        return True

    def _domain_object(self, words, i):
        """Is the noun at `words[i]` the object of a domain word ('of these cities'), reached over determiners and
        at most one other word, with no grouping cue before that word ('each of the cities')?"""
        other, j = 0, i - 1
        while j >= 0 and words[j] not in self._DOMAIN_LINK:
            word = words[j]
            if word in self._GROUPING_CUES or word in self._RANKING_WORDS or word.isdigit():
                return False
            if word not in self._DETERMINERS:
                other += 1
                if other > 1:
                    return False
            j -= 1
        return j >= 0 and not (j and words[j - 1] in self._GROUPING_CUES)

    def _divide(self, low, numeric):
        """Two-measure ratio ('profit to revenue ratio', 'revenue per order'): needs TWO numeric columns named in the
        question + a ratio cue. Returns (num_col, den_col) in question order, or None."""
        if len(numeric) < 2 or not re.search(r'\bper\b|\bratio\b|divided by|as a (?:fraction|percentage|percent) of', low):
            return None
        toks = re.findall(r'[a-z]+', low)
        named = [c for c in sorted(numeric, key=lambda c: toks.index(c.lower()) if c.lower() in toks else 1e9)
                 if c.lower() in toks]
        return (named[0], named[1]) if len(named) >= 2 else None

    def plan(self, question, table, prims=None, used=None):
        """question + one table -> ordered list of primitive steps (the view DAG), in the canonical analytics pipeline
        order: filters (exclusion, temporal, a row threshold) -> aggregate (+ a derive: yoy / running / share /
        divide) -> having -> rank (top-N / sort). `prims` is the primitive SET from the learned head; the newer
        primitives + all operands are read by transparent regex/value-matching seams. Operands are extracted
        regardless of where the structure decision comes from."""
        low = " " + question.lower() + " "
        cols = table["columns"]; rows = table["rows"]; typ = self._types(cols, rows)
        numeric = [c for c in cols if typ[c] == "num" and not is_surrogate_key(c)]
        times = [c for c in cols if typ[c] == "time"]
        texts = [c for c in cols if typ[c] == "text"]
        # A comparison binds the attribute it names. On a row attribute other than the aggregated measure it keeps
        # rows before aggregating ('total sales in big cities with population over 1,000,000' is 350, not a
        # threshold on the total sales); on the measure it thresholds the aggregate ('cities with total sales over
        # 100'). It once bound the metric whatever it named, and served an empty per-city table (2026-09-28).
        threshold = self._threshold(low, numeric)
        bound, _, _, span, aggregated = threshold or (None, None, None, None, False)
        unbound = low if bound is None else low[:span[0]] + " " + low[span[1]:]   # the question minus that clause
        measure = bound if aggregated else self._pick_measure(
            unbound, [c for c in numeric if c != bound or self._names_attribute(c, unbound)] or numeric, question)
        intent = self._intent(low, table, question)
        # A measure the question names with cells that are not numbers cannot be aggregated, and no other column
        # stands in for it: one '118 (accounting says 11800)' amount once made "the total amount of GBP orders in
        # Europe" a sum of the cities' populations (planted-text test, 2026-10-08).
        if intent in ("SUM", "AVG") and not any(self._names_attribute(c, unbound) for c in numeric):
            for c in texts:
                cells = (unreadable_cells([r[cols.index(c)] for r in rows])
                         if self._names_attribute(c, unbound) else None)
                if cells:
                    raise UnreadableMeasure(unreadable_measure_reason(c, cells, intent))
        op = intent or "SUM"            # group_agg still needs an aggregator (e.g. SUM within a YoY pre-agg)
        row_threshold = (threshold if bound is not None and not aggregated and (op == "COUNT" or bound != measure)
                         else None)
        # operands (extracted regardless of where the structure decision comes from)
        excl_val = self._excluded_value(low, texts, cols, rows)
        time_preds = self._time_pred(low, times)
        having_pred = threshold[1:3] if threshold and row_threshold is None else None
        divide_pair = self._divide(low, numeric)
        topn_op = self._topn(low); sort_desc = self._sort(low)
        # A noun named only as the set an aggregate runs over is not a grouping: 'the average population of these
        # cities' and 'total sales in big cities with population over 1,000,000' are one number each. Every
        # mention used to group, so those questions were served as per-city tables (2026-09-28). A question that
        # asks for no aggregate ('which cities have a population over ...') lists the noun, and a threshold on
        # each group's aggregate needs the groups, so both keep it.
        row_nouns = measured_rows(question, frozenset(word for c in cols for word in re.findall(r"[a-z]+", c.lower())))
        dims = [c for c in self._dims(low, texts, table.get("sources") or [table["name"]],
                                      row_nouns | counted_rows(question))
                if not (excl_val and c == excl_val[0])
                and not (intent and having_pred is None
                         and self._aggregated_over(c, low, span[0] if row_threshold else None))]
        # structure: ALL 10 primitives come from the LEARNED head when present (operands are still extracted above);
        # else the encoder-free regex/value seams. The operand-bearing steps below additionally require their operand
        # (a year, a threshold, two measures) to actually build — head structure + the parsed literal.
        if prims is not None:
            has = {p: (p in prims) for p in ("EXCL", "RATIO", "TOPN", "SHARE", "GROUP",
                                             "TIME", "HAVING", "SORT", "DIVIDE", "RUNNING")}
            # operand-gated primitives: OR the learned head with the explicit lexical/operand cue, so a rare synonym
            # the head misses ('without') is still caught — a false positive is gated by the operand requirement
            # below, so the encoder path is never worse than the regex path.
            has["EXCL"] = has["EXCL"] or bool(self.EXCL_TRIGGER.search(low))
            has["TIME"] = has["TIME"] or bool(time_preds)
            has["HAVING"] = has["HAVING"] or having_pred is not None
            has["DIVIDE"] = has["DIVIDE"] or divide_pair is not None
            # A ranking changes WHICH rows answer (a LIMIT, a grouping by the first text column), so the head
            # alone cannot add one: the question must rank ('top', 'highest', 'most', 'sorted by'). The head fired
            # TOPN on 'total amount in North America in USD' and composed the top 3 customers (2026-09-30).
            ranked = (topn_op is not None or sort_desc is not None
                      or bool(set(re.findall(r"[a-z]+", low)) & self._RANKING_WORDS))
            has["TOPN"] = has["TOPN"] and ranked
            has["SORT"] = has["SORT"] and ranked
            # a named grouping dimension in the question IS a group-by, even when the head misfires (it fires TIME,
            # not GROUP, on 'by continent'). dims are column names actually mentioned, so this only adds a real
            # group-by ("total sales by continent" -> group by continent), never a spurious one on a scalar query.
            has["GROUP"] = has["GROUP"] or bool(dims)
        else:
            has = {"EXCL": bool(self.EXCL_TRIGGER.search(low)),
                   "RATIO": bool(re.search(r'year over year|year-over-year|\byoy\b|\bgrowth\b', low)),
                   "TOPN": topn_op is not None,
                   "SHARE": bool(re.search(r'\bshare\b|percentage|percent|proportion|% of', low)),
                   "GROUP": bool(dims),
                   "TIME": bool(time_preds),
                   "HAVING": having_pred is not None,
                   "DIVIDE": divide_pair is not None,
                   "RUNNING": bool(re.search(r'cumulative|running total|running sum|accumulat|\bto date\b', low)),
                   "SORT": sort_desc is not None}
        has["SORT"] = has["SORT"] and not has["TOPN"]       # rank is top-N XOR sort

        steps = []; src = table["name"]
        if used is None:
            used = set()

        def add(step, label):
            nonlocal src
            step.update(out=self._vname(step["op"], used), src=src, label=label); steps.append(step); src = step["out"]

        if has["EXCL"] and excl_val:                        # 1. categorical row exclusion
            add({"op": "filter", "conds": [(excl_val[0], "<>", excl_val[1])]},
                f"exclude {excl_val[0]} = {excl_val[1]!r}")
        if has["TIME"] and time_preds:                      # 2. temporal filter
            add({"op": "time_filter", "conds": time_preds},
                "where " + " and ".join(f"{c} {o} {v}" for c, o, v in time_preds))
        if row_threshold:                                   # 2b. a row threshold on the attribute it names
            column, cmp, value, _, _ = row_threshold
            add({"op": "filter", "conds": [(column, cmp, value)]}, f"where {column} {cmp} {value}")

        key = dims[0] if dims else self._pick_dim(low, texts, question)
        metric = None; rank_select = None
        if has["RATIO"] and times and measure:              # 3a. aggregate -> YoY growth (key optional)
            tcol = times[0]; gby = [key, tcol] if key else [tcol]
            add({"op": "group_agg", "by": gby, "aggs": [(op, measure, measure)]}, f"{op}({measure}) by {', '.join(gby)}")
            add({"op": "yoy", "key": key, "time": tcol, "measure": measure, "outcol": "yoy"},
                f"YoY growth of {measure}" + (f" by {key}" if key else ""))
            metric = "yoy"; rank_select = [key, "yoy"] if key else ["yoy"]
        elif has["RUNNING"] and times and measure:          # 3b. aggregate -> running (cumulative) total (key optional)
            tcol = times[0]; gby = [key, tcol] if key else [tcol]
            add({"op": "group_agg", "by": gby, "aggs": [(op, measure, measure)]}, f"{op}({measure}) by {', '.join(gby)}")
            add({"op": "running", "key": key, "time": tcol, "measure": measure, "outcol": "cumulative"},
                f"running total of {measure}" + (f" by {key}" if key else ""))
            metric = "cumulative"; rank_select = [key, tcol, "cumulative"] if key else [tcol, "cumulative"]
        elif has["DIVIDE"] and divide_pair:                 # 3c. aggregate two measures -> ratio of them
            num, den = divide_pair
            by = dims if (has["GROUP"] or has["TOPN"] or has["SORT"]) else []
            add({"op": "group_agg", "by": by, "aggs": [(op, num, num), (op, den, den)]},
                f"{op}({num}), {op}({den})" + (f" by {', '.join(by)}" if by else ""))
            add({"op": "divide", "num": num, "den": den, "outcol": "ratio", "keep": by}, f"{num} / {den}")
            metric = "ratio"; rank_select = (by + ["ratio"]) if by else None
        elif op == "COUNT" or (measure is None and (has["GROUP"] or has["TOPN"] or has["SORT"])):
            by = dims if (has["GROUP"] or has["TOPN"] or has["SORT"]) else []   # 3d. ROW COUNT -> COUNT(*) (no measure needed)
            if (has["TOPN"] or has["SORT"]) and not by and texts:
                by = [self._pick_dim(low, texts, question)]
            add({"op": "group_agg", "by": by, "aggs": [("COUNT", "*", "count")]},
                "COUNT(*)" + (f" by {', '.join(by)}" if by else ""))
            metric = "count"; rank_select = (by + ["count"]) if by else None
        elif measure is not None:                           # 3e. plain aggregate (SUM / AVG)
            by = dims if (has["GROUP"] or has["TOPN"] or has["SORT"]) else []
            if (has["TOPN"] or has["SORT"]) and not by and texts:   # 'top products' w/ no explicit 'by X' -> the entity
                by = [texts[0]]
            add({"op": "group_agg", "by": by, "aggs": [(op, measure, measure)]},
                f"{op}({measure})" + (f" by {', '.join(by)}" if by else ""))
            metric = measure; rank_select = (by + [measure]) if by else None

        if has["SHARE"] and dims and metric:                # 4. share of total
            add({"op": "share", "dim": dims[0], "measure": metric, "outcol": "share"}, f"share of total {metric}")
            metric = "share"; rank_select = [dims[0], "share"]
        if has["HAVING"] and having_pred and metric:        # 5. post-aggregate threshold on the metric
            add({"op": "having", "conds": [(metric, having_pred[0], having_pred[1])]},
                f"having {metric} {having_pred[0]} {having_pred[1]}")
        if has["TOPN"] and metric:                          # 6. rank: top-N (with limit)
            n, desc = topn_op if topn_op else (3, True)
            add({"op": "topn", "order": metric, "desc": desc, "n": n, "select": rank_select},
                f"{'top' if desc else 'bottom'} {n} by {metric}")
        elif has["SORT"] and metric:                        # 6. rank: sort (no limit)
            desc = sort_desc if sort_desc is not None else True   # head may fire SORT w/o an explicit direction word
            add({"op": "sort", "order": metric, "desc": desc, "n": None, "select": rank_select},
                f"sort by {metric} {'desc' if desc else 'asc'}")
        return steps

    # ---------------- execution: materialize the view stack ----------------
    def _coerce(self, v, t):
        if v is None or str(v).strip() == "":
            return None
        if t == "num":
            return sqlite_numeric(v, "REAL")
        if t == "time":
            return sqlite_numeric(v, "INTEGER")
        return str(v)

    def _load(self, con, table):
        cols = table["columns"]; rows = table["rows"]; typ = self._types(cols, rows)
        aff = {c: ("TEXT COLLATE decimal" if typ[c] == "num" else "INTEGER" if typ[c] == "time" else "TEXT")
               for c in cols}
        con.execute(f'CREATE TABLE {q(table["name"])} (' + ", ".join(f'{q(c)} {aff[c]}' for c in cols) + ')')
        ins = f'INSERT INTO {q(table["name"])} VALUES (' + ", ".join("?" * len(cols)) + ')'
        for r in rows:
            con.execute(ins, [self._coerce(r[ci], typ[cols[ci]]) for ci in range(len(cols))])
        con.commit()

    # Logical, self-describing view names (so the generated SQL reads "FROM filtered" not "FROM b2" — easier to
    # debug). Deduped with a numeric suffix when an op repeats (two filters -> filtered, filtered_2).
    _VNAME = {"join": "combined", "world_join": "knowledgebase_lookup", "world_filter": "filtered",
              "filter": "filtered", "time_filter": "date_filtered", "having": "filtered", "group_agg": "total",
              "topn": "top_results", "sort": "sorted", "yoy": "year_over_year", "running": "running_total",
              "divide": "ratio", "share": "share"}

    def _vname(self, op, used):
        base = self._VNAME.get(op, "step"); n = base; i = 2
        while n in used:
            n = f"{base}_{i}"; i += 1
        used.add(n); return n

    def _materialize(self, con, name, sql, op, label):
        """create a view, execute it, and return its trace dict (name, op, label, sql, columns, rows)."""
        con.execute(f'CREATE VIEW {q(name)} AS {sql}')
        cur = con.execute(f'SELECT * FROM {q(name)}')
        cols = [d[0] for d in cur.description]; rows = [list(r) for r in cur.fetchall()]
        types = self._types(cols, rows)
        for row in rows:
            for index, column in enumerate(cols):
                if types[column] == "num" and isinstance(row[index], str):
                    row[index] = wire_decimal(parse_decimal(row[index], enforce_input_bounds=False))
        return {"name": name, "op": op, "label": label, "sql": sql, "columns": cols, "rows": rows}

    def _world_link(self, low, table, world):
        """Find the base column that LINKS to the world entity (>=half its values are world keys, e.g. cities), and
        decide the world predicate: a world DIMENSION value named in the question ('France'->country, 'Europe'->
        continent) gives a FILTER; otherwise, if a world ATTRIBUTE NAME is mentioned ('population'/'currency'), the
        join still happens (so the attribute is available, e.g. as a measure) with NO filter. NUMERIC attributes
        (population/mass) are measures, never filter dimensions. -> (link, world_key, keep, dim|None, value|None) | None."""
        wkey = world["columns"][0]
        wvals = {str(r[0]).lower() for r in world["rows"] if r and r[0] is not None}
        link = None
        for ci, c in enumerate(table["columns"]):
            vals = [str(r[ci]).lower() for r in table["rows"] if ci < len(r) and r[ci] is not None]
            if vals and sum(v in wvals for v in vals) >= 0.5 * len(vals):
                link = c; break
        if link is None:
            return None
        toks = set(re.findall(r'[a-z]+', low))
        keep = world["columns"][1:]
        for di in range(1, len(world["columns"])):           # a world VALUE in the question -> FILTER (skip numerics)
            colvals = [r[di] for r in world["rows"] if r[di] is not None]
            if colvals and all(self._isnum(v) for v in colvals):
                continue
            for r in world["rows"]:
                if r[di] is not None and str(r[di]).lower() in toks:
                    return (link, wkey, keep, world["columns"][di], str(r[di]))
        if any(self._mentions(c, low) for c in keep):        # no filter value, but an attribute is needed -> join only
            return (link, wkey, keep, None, None)
        return None

    def _sql(self, s):
        op = s["op"]
        if op in ("filter", "time_filter", "having"):       # all three are row predicates (pre-agg or post-agg)
            return filter_view(s["src"], s["conds"])
        if op == "group_agg":
            return group_agg_view(s["src"], s["by"], s["aggs"])
        if op == "yoy":
            return yoy_view(s["src"], s["key"], s["time"], s["measure"], s["outcol"])
        if op == "running":
            return running_view(s["src"], s["key"], s["time"], s["measure"], s["outcol"])
        if op == "divide":
            return divide_view(s["src"], s["num"], s["den"], s["outcol"], s.get("keep"))
        if op == "share":
            return share_view(s["src"], s["dim"], s["measure"], s["outcol"])
        if op in ("topn", "sort"):
            return topn_view(s["src"], s["order"], s["desc"], s["n"], s["select"])
        raise ValueError(f"unknown primitive {op}")

    def run(self, tables, question, world=None):
        """Build + execute the view stack. A BASE relation is formed first when there is more than one uploaded table
        (FK join) and/or a world-meaning filter (world join + filter); the analytical primitives then stack on that
        flattened base. Returns the final answer + the full trace (every view: name, op, SQL, rows)."""
        low = " " + question.lower() + " "
        con = sqlite3.connect(":memory:")
        register_sqlite_decimal(con)
        try:
            for t in tables:
                self._load(con, t)
            if world is not None:
                self._load(con, world)
            base = []; used = set(); cur = tables[0]
            jp = None
            wf = None
            if len(tables) > 1:                              # base A: FK join of the uploaded tables
                jp = join_plan(tables, discover_fks(tables))
                if jp:
                    fact, joins = jp
                    cur = self._materialize(con, self._vname("join", used), join_view(fact, joins), "join",
                                            "join " + " + ".join([fact] + [d for d, *_ in joins]))
                    base.append(cur)
            # base A': a plain equality filter on an UPLOADED text column ('total amount in Chennai' -> city='Chennai').
            # The value is already in the data, so filter it DIRECTLY — no world model. Runs on the uploaded columns
            # BEFORE the world join adds a 'country'/'continent', so it never re-matches a world attribute the
            # world_filter below already applied ('total amount in France' has no 'France' cell -> world handles it).
            vtyp = self._types(cur["columns"], cur["rows"])
            vf = self._value_filter(low, [c for c in cur["columns"] if vtyp[c] == "text"], cur["columns"], cur["rows"])
            if vf:
                cur = self._materialize(con, self._vname("filter", used), filter_view(cur["name"], [(vf[0], "=", vf[1])]),
                                        "filter", f"where {vf[0]} = {vf[1]!r}")
                base.append(cur)
            world_grounding = None                           # (supplied_attrs, own_columns, wcol, value, world_filtered)
            unmatched = None
            if world is not None:                            # base B: world-meaning join (+ optional filter on a world dim)
                own_cols = {str(c).lower() for c in cur["columns"]}   # what the UPLOAD already provides, PRE world join
                wf = self._world_link(low, cur, world)
                if wf:
                    link, wkey, keep, wcol, value = wf
                    # A row whose link value is no world entity gets no world attribute, so a world filter leaves it
                    # out: "Brussels (a city in Germany)" dropped an order from "total amount in Belgium" without a
                    # word (planted-text test, 2026-10-08). The answer says so. A link needs most of its values
                    # known (_world_link), so declining when most are unknown is the delegate's (KnowledgeQuery).
                    from engine.knowledge_query import unmatched_rows
                    li = cur["columns"].index(link)
                    known = {str(r[0]) for r in world["rows"] if r and r[0] is not None}
                    named = [r for r in cur["rows"] if li < len(r) and r[li] is not None and str(r[li]).strip()]
                    unmatched = unmatched_rows(cur, link, named, [str(r[li]) in known for r in named], [], link)
                    cur = self._materialize(con, self._vname("world_join", used), world_join_view(cur["name"], link, world["name"], wkey, keep),
                                            "world_join", f"join {cur['name']} to the world on {link}")
                    base.append(cur)
                    world_filtered = False
                    if wcol and value is not None:           # a world VALUE was named -> filter; else attribute-only join
                        cur = self._materialize(con, self._vname("world_filter", used), filter_view(cur["name"], [(wcol, "=", value)]),
                                                "world_filter", f"where {wcol} = {value!r}")
                        base.append(cur)
                        world_filtered = True
                    world_grounding = (list(keep or []) + ([wcol] if wcol else []), own_cols, wcol, value, world_filtered)
            table = {"name": cur["name"], "columns": cur["columns"], "rows": cur["rows"],
                     "sources": [t["name"] for t in tables]}   # the uploaded sheets under the view stack
            prims = self.reader.present(question) if self.reader else None   # learned readout (or None=heuristic)
            try:
                steps = self.plan(question, table, prims, used)
            except UnreadableMeasure as refusal:
                return {"question": question, "n_steps": len(base), "plan": [v["op"] for v in base],
                        "primitives": sorted(prims) if prims is not None else None, "answer": None,
                        "clarify": True, "reason": str(refusal),
                        "bindings": {"uploaded_join": jp, "value_filter": vf, "world_join": wf,
                                     "base_table": table, "steps": []},
                        "views": list(base), "world_dependency": None}
            views = list(base)
            for s in steps:
                views.append(self._materialize(con, s["out"], self._sql(s), s["op"], s.get("label")))
            final = views[-1] if views else None
            # EXPLICIT world-dependency record for the router. A world_join proves world data was JOINED, not that
            # it was NECESSARY. Necessity has three independent sources:
            #   (a) a world-supplied ATTRIBUTE the upload lacks that the QUESTION names and the ANSWER actually
            #       uses (in the final columns). BOTH conditions are required: this engine can inject a world
            #       attribute into its own group-by, and an attribute nobody asked for must not make the join
            #       that supplied it "necessary" — that circularity once made compose own a plain row count
            #       ('Count all non-empty Order ID rows...') as GROUP BY customer, country (2026-09-14).
            #   (b) a world FILTER whose value could NOT already be bound directly against uploaded data. The direct
            #       value-filter `vf` (base A' above) binds an uploaded (column, value) when the value is present, so
            #       a world_filter on the SAME value is REDUNDANT -> own-data. Uploaded ABBREVIATIONS ('FR') never
            #       bind 'France' directly, so `vf` is empty there and world resolution stays necessary.
            #   (c) a world ATTRIBUTE the upload lacks that a row threshold compares ('cities with population over
            #       1,000,000'): the kept rows depend on it though the answer does not show it.
            # We do NOT treat world_filtered alone as necessary (that was the bug: a redundant world join over an
            # already-satisfiable filter would wrongly claim the query).
            world_dependency = None
            if world_grounding is not None:
                supplied, own_cols, wcol, value, world_filtered = world_grounding
                supplied = sorted({str(a) for a in supplied if a})
                final_cols = {str(c).lower() for c in (final["columns"] if final else [])}
                used_necessary = sorted(a for a in supplied if a.lower() not in own_cols
                                        and a.lower() in final_cols and self._names_attribute(a, low))
                direct_bound_same_value = bool(vf and value is not None
                                               and str(vf[1]).strip().lower() == str(value).strip().lower())
                world_filter_necessary = bool(world_filtered and not direct_bound_same_value)
                world_threshold = sorted({column for step in steps if step["op"] == "filter"
                                          for column, cmp, _ in step["conds"]
                                          if cmp in (">", ">=", "<", "<=") and column in supplied
                                          and column.lower() not in own_cols})
                world_dependency = {
                    "supplied": supplied, "own_columns": sorted(own_cols), "necessary": used_necessary,
                    "world_filtered": world_filtered, "world_filter_necessary": world_filter_necessary,
                    "direct_filter": (list(vf) if vf else None), "world_threshold": world_threshold,
                    "is_necessary": bool(world_filter_necessary or used_necessary or world_threshold),
                    "filter_attribute": wcol, "filter_value": value,
                }
        finally:
            con.close()
        return {"question": question, "n_steps": len(views), "plan": [v["op"] for v in views],
                "primitives": sorted(prims) if prims is not None else None,
                "answer": ({"columns": final["columns"], "rows": final["rows"]} if final else None),
                "bindings": {"uploaded_join": jp, "value_filter": vf, "world_join": wf,
                             "base_table": table, "steps": steps},
                "views": views, "world_dependency": world_dependency, "unmatched": unmatched}
