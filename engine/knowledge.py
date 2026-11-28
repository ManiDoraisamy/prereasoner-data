"""KnowledgeReasoner = ComposedKnowledgeQuery (composition + world + aggregates) PLUS a geo NEARBY primitive (lat/lng
distance search), ADDITIVELY (it wraps, never modifies, the composed planner — no regression risk).

A "near/around/closest <city>" question resolves the reference city to its lat/lng (public.settlement, the
clean ~174k-row geo source) and returns the nearest world cities by haversine distance. Everything else
delegates to ComposedKnowledgeQuery unchanged (population ranking, aggregates, hybrid, clarify, composition).

serve() also owns one signal for the conversational layer: the PRESENT tag (_tag_present flags a real answer whose
phrasing is emotional/human, so the UI presents it in words). Its detector lives on ComposedKnowledgeQuery;
KnowledgeReasoner applies it across the geo + composed paths. No word list stands before the reader: every question
asked of a sheet is read (until 2026-11-28 a list of English intent words decided first whether a message was a data
question at all; as served it turned away none of 355 dev questions, and no evaluation below it could see it).
"""
from __future__ import annotations
import re

from engine.calculations import (
    ComputationEvidence,
    assess_calculations,
    calculation_clarify,
    detect_calculations,
)
from engine.calculations.registry import attach_calculation_evidence
from engine.knowledge_compose import ComposedKnowledgeQuery
from engine.pg import _pg
from engine.prepared import request_database
from engine.relations import request_memo
from engine.request_state import request_scope
from engine.sql_expansion import FUNCTION_WORDS
from engine.sql_schema import SchemaGraph
from engine.query_contract import WORLD_ROUTE_ASKS
from engine.tables import PROGRAM_MODEL

NEAR = re.compile(r"\b(near(?:est|by)?|closest|around|close to)\b", re.I)
STOP = {"cities", "city", "towns", "town", "places", "show", "find", "list", "me", "the", "biggest", "big",
        "largest", "major", "to", "of", "in", "which", "what", "are", "is", "by", "with", "and"}
# The words a question about places may put before its nearness word: the nouns of the places the lookup
# returns, and words that only ask for, count or size them the way its query does ("show me the 3 biggest
# towns near Lyon": a limit and the big-city population floor). Any other word there names something the
# lookup cannot give: what the question is about ("orders near Paris", "how many cities are near Paris") or a
# kind or order of place its query does not select ("capitals near Paris", "the smallest towns near Lyon").
PLACE_QUESTION_WORDS = frozenset(STOP | FUNCTION_WORDS | {
    "place", "settlement", "settlements", "top", "some", "few", "other", "large"})
HAVERSINE = ("6371*acos(greatest(-1,least(1, cos(radians(%s))*cos(radians(p.lat))*cos(radians(p.lng)-radians(%s))"
             "+sin(radians(%s))*sin(radians(p.lat)))))")


class KnowledgeReasoner:
    def __init__(self):
        self.composed = ComposedKnowledgeQuery()
        self.qw = self.composed.qw                                              # expose KnowledgeQuery (server warmup: MODEL.qw._spacy())

    def serve(self, tables, question, sub, as_of=None, emit=None, explicit_fks=(), dataset_semantics=(),
              decomposition=None, active_sheet=None):
        # Questions run concurrently on this one object: the request gets its own database (engine/prepared.py),
        # connections and question text (engine/request_state.py), and its paths derive its tables once
        # (relations.memoized). active_sheet: the tab the client has in view, a table's name, which the own-data
        # path's records name (engine/prepared.py RequestDatabase.active_sheet).
        with request_scope(), request_memo(), request_database(active_sheet):
            try:
                return self._serve(tables, question, sub, as_of=as_of, emit=emit, explicit_fks=explicit_fks,
                                   dataset_semantics=dataset_semantics, decomposition=decomposition)
            finally:
                self.qw.end_request()       # close the resolution connection this request opened

    def retry(self, tables, question, active_sheet=None, as_of=None, explicit_fks=()):
        """The retry of an answer (the person's, or the one a client sends for an answer served very likely wrong): the
        own-data planner's retry (engine/tables.py TableQuery.retry_answer,
        engine/retry.py) on the request's database, over the workbook its links connect (``link_request``, as
        ``_serve`` registers them), so a retried question reads what the request's columns reach."""
        with request_scope(), request_memo(), request_database(active_sheet):
            self.qw.begin_request()
            try:
                self.qw.link_request(as_of, explicit_fks)
                return self.qw.q11.retry_answer(tables, question, explicit_fks=explicit_fks)
            finally:
                self.qw.end_request()

    def settled(self, response, tables, question, active_sheet=None, as_of=None, explicit_fks=()):
        """``response`` (a ``serve`` answer) with the retry it is owed done in process (engine/tables.py
        TableQuery.settled), over the workbook the request's links connect."""
        from engine.tables import owes_retry
        if not owes_retry(response):
            return response
        again = self.retry(tables, question, active_sheet=active_sheet, as_of=as_of, explicit_fks=explicit_fks)
        return again if again.get("result") is not None or again.get("clarify") is True else response

    def _serve(self, tables, question, sub, as_of=None, emit=None, explicit_fks=(), dataset_semantics=(),
               decomposition=None):
        self.qw.begin_request()             # fresh request-scoped memo for shared-knowledge lookups
        self.qw.link_request(as_of, explicit_fks)   # the own-data path's workbook gains what the request's tables reach
        if NEAR.search(question or "") and self._asks_for_places(question):
            r = self._nearby(question)
            if r:                                                           # geo nearby handled (server emits its result)
                r = self._tag_present(r, question, tables)
                return self._verify_calculations(r, tables, question, explicit_fks)
        self.qw.begin_typing()                                              # capture the model's per-column typing
        try:                                                                # evidence emitted while this serve routes
            res = (self.composed.serve(
                tables, question, sub, as_of=as_of, emit=emit, explicit_fks=explicit_fks,
                dataset_semantics=dataset_semantics, decomposition=decomposition,
            ) if explicit_fks else self.composed.serve(
                tables, question, sub, as_of=as_of, emit=emit,
                dataset_semantics=dataset_semantics, decomposition=decomposition,
            ))
        finally:
            typing = self.qw.take_typing()
        res = self._upload_stands(res, tables, question, sub, as_of, explicit_fks, dataset_semantics, decomposition)
        res = self._tag_present(res, question, tables)
        res = self._verify_calculations(res, tables, question, explicit_fks)
        return self._attach_typing(res, typing)

    def _upload_stands(self, res, tables, question, sub, as_of, explicit_fks, dataset_semantics, decomposition):
        """A world route's failure never takes the place of the upload's answer (owner, 2026-11-21: "never ask back
        unless the question is wrong. You can't push your failure to users"). Where the composed stack returns an
        error with no result, an answer of no rows at all, or a world route's question back about what it could not do
        itself (names its lookup could not match, a word its query left out: query_contract.WORLD_ROUTE_ASKS), the own-data path's answer is
        served in its place, with its mark, and its retry is owed as for any answer. On the frozen government
        workbooks 3 of 212 questions came back that way, and on fresh dev's first 60, 6 (as served, 2026-11-28). A
        question back that says the question or the sheet is wrong stands: no rows match it, the measure it names is
        no number, a calculation is unmet, its parts are asked for. So does the failure, when the upload has no answer
        either."""
        if (explicit_fks or decomposition or not tables or not isinstance(res, dict) or res.get("model") == PROGRAM_MODEL
                or res.get("decomposition_required")):
            return res
        result = res.get("result")
        nothing = isinstance(result, dict) and not result.get("rows") and not res.get("clarify") and not res.get("error")
        failed = ((res.get("error") and not result) or nothing
                  or (res.get("clarify") is True and res.get("model") in WORLD_ROUTE_ASKS))
        if not failed:
            return res
        own = self.qw.serve(tables, question, as_of=as_of, schema=sub, dataset_semantics=dataset_semantics, upload_alone=True)
        # its answer, its marked answer with no rows (owed the retry), or its own word that the question matches nothing
        if isinstance(own, dict) and (own.get("result") is not None or isinstance(own.get("check"), dict) or own.get("clarify")):
            return own
        return res

    def _verify_calculations(self, res, tables, question, explicit_fks):
        """Apply one route-independent gate to every registered calculation intent."""
        if not isinstance(res, dict) or res.get("error"):
            return res
        # A clarification has no number to certify. The one it replaces is about words the query
        # dropped, usually the calculation's own phrase ("in euros"); a filter that matched no rows,
        # a decomposition request or an unmatched name stands. "total budget in Africa", asked in US
        # dollars, matched no rows and was answered "the selected planner supplied no typed
        # calculation evidence" (Chrome gate, 2026-10-04).
        if res.get("clarify") and not res.get("dropped"):
            return res
        if not detect_calculations(question):
            return res
        assessments = tuple(res.get("calculations") or ())
        if not assessments and res.get("model") == PROGRAM_MODEL:
            return res      # the own-data path's reading certifies its calculation (engine/tables.py _serve_ast)
        if not assessments:
            # A route that cannot expose typed arithmetic must fail closed. Its SQL/result may be useful
            # context for clarification, but it is not evidence that a requested calculation occurred.
            graph = SchemaGraph.from_tables(tables or (), explicit_fks or ())
            assessments = assess_calculations(
                question,
                tables,
                graph,
                ComputationEvidence.unverified(),
            )
            assessments = tuple({
                **assessment,
                "planner_fallback": "route supplied no typed calculation evidence",
            } for assessment in assessments)
        if not assessments:
            return res
        attach_calculation_evidence(res, assessments)
        if any(assessment.get("status") != "satisfied" for assessment in assessments):
            return calculation_clarify(question, res, assessments)
        return res

    def _attach_typing(self, res, typing):
        """Surface the LEARNED world-grounding decision: for each column the trained model typed, WHICH
        schema.org properties fired to decode its family and whether it grounded to a world table. Additive +
        best-effort (mirrors _tag_present) — an own-data query types nothing, so `typing` is simply absent."""
        if isinstance(res, dict) and typing:
            res["typing"] = typing
        return res

    def _tag_present(self, res, question, tables=None):
        """PRESENT signal: the answer is real, but the phrasing is emotional/human — flag it so the UI routes
        the computed answer + derivation through Gemini to present it in words (derivation still shown in the
        panel). Only when signaled (human tone) — plain data queries stay raw, zero Gemini cost. Applied to
        BOTH the geo path and the composed path. Best-effort — never breaks the answer."""
        try:
            if (isinstance(res, dict) and res.get("result") and not res.get("clarify")
                    and not res.get("error")
                    and self.composed._human_tone(question, tables)):
                res["present"] = True
        except Exception as e:                                              # noqa: BLE001 — never break the answer
            print(f"present flag skipped: {type(e).__name__}", flush=True)
        return res

    @staticmethod
    def _asks_for_places(question):
        """Whether a question with a nearness word asks for the places near a place, the one thing the
        settlement lookup answers ("big cities near Paris", "what is near Lyon"). Any nearness word used to send
        a question there before the upload was read: "orders near Paris" over an orders sheet came back as the
        cities near Paris, and "sales around Christmas" looked Christmas up as a place (release review,
        2026-10-07). A question about anything else goes on to the composed path, which answers it or says
        why not."""
        # Every script's words count: "売上 near Paris" read as no words at all before (release review,
        # revision 2). Only a stray ASCII letter ("what's") is not a word.
        before = (question or "")[:NEAR.search(question or "").start()]
        return all((len(word) < 2 and word.isascii()) or word in PLACE_QUESTION_WORDS
                   for word in re.findall(r"[^\W\d_]+", before.lower()))

    def _ref_and_limit(self, question):
        q = question or ""
        m = NEAR.search(q)
        tail = q[m.end():] if m else ""                                     # text AFTER 'near' = the reference
        cand = [w for w in re.findall(r"[A-Za-z][A-Za-z .'-]*[A-Za-z]|[A-Za-z]", tail)]
        ref = " ".join(w for w in " ".join(cand).split() if w.lower() not in STOP).strip(" .")
        lim = re.search(r"\b(\d{1,3})\b", q)
        big = bool(re.search(r"\b(big|biggest|large|largest|major)\b", q, re.I))
        return ref, (int(lim.group(1)) if lim else 5), big

    def _nearby(self, question):
        ref, limit, big = self._ref_and_limit(question)
        if not ref:
            return None
        cn = _pg(); cur = cn.cursor()
        try:
            cur.execute("SELECT name,lat,lng,qid FROM public.settlement WHERE lower(name)=lower(%s) "
                        "AND lat IS NOT NULL AND lng IS NOT NULL ORDER BY population DESC NULLS LAST, qid LIMIT 1",
                        (ref,))
            row = cur.fetchone()
            if not row:
                return None
            name, lat, lng, qid = row
            minpop = 150000 if big else 1
            # NEARBY must return DISTINCT nearby cities, not the reference's own pieces or junk rows:
            #  - admin_qid <> the reference's qid: public.settlement carries administrative SUBDIVISIONS of a city
            #    (Paris's 13th/15th/.../20th arrondissement + "Paris metropolitan area" all have admin_qid = Q90,
            #    Paris's own qid) and each is >150k, so the 'big' population floor doesn't exclude them. Dropping rows
            #    whose admin parent IS the reference removes those self-pieces -> real other cities (Reims, Lille,
            #    Ghent, Brussels…). The 'arrondissement' name token is belt-and-suspenders for the literal symptom.
            #  - name !~ '^Q[0-9]+$' (and NOT NULL): some settlement rows have a raw QID as their name
            #    (e.g. Q122687396 near Tokyo) — never show a QID string as a city.
            # The order is the exact distance, then the entity: places the same whole number of km away were
            # cut at LIMIT in no stated order when it was the rounded km (release review, 2026-10-07).
            sql = (f'SELECT name, country, population, round(distance::numeric, 0) AS km FROM ('
                   f'SELECT p.name, p.country, p.population, p.qid, {HAVERSINE} AS distance '
                   f'FROM public.settlement p WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL '
                   f'AND p.population > %s AND lower(p.name) <> lower(%s) '
                   f"AND p.name IS NOT NULL AND p.name !~ '^Q[0-9]+$' "
                   f"AND p.name !~* 'arrondissement' "
                   f'AND (p.admin_qid IS NULL OR p.admin_qid <> %s)) nearby '
                   f'ORDER BY distance ASC, qid ASC, name ASC LIMIT %s')
            cur.execute(sql, (lat, lng, lat, minpop, name, qid, limit))
            rows = cur.fetchall()
            disp = (f"SELECT name, country, population, round(distance_km) FROM world cities "
                    f"ORDER BY haversine(lat,lng, {name}@{lat:.3f},{lng:.3f}) ASC LIMIT {limit}")
            return {"question": question, "model": "engine - geo nearby (lat/lng haversine)",
                    "reference": {"name": name, "qid": qid, "lat": lat, "lng": lng},
                    "sql": disp,
                    "result": {"columns": ["name", "country", "population", "km"], "rows": [list(r) for r in rows]}}
        finally:
            cn.close()


def main():
    import os
    if not os.environ.get("KB_PG_PASSWORD"):
        print("set KB_PG_PASSWORD"); return
    q = KnowledgeReasoner()
    r = q._nearby("big cities near Paris")
    if r:
        print("ref:", r["reference"]["name"], r["reference"]["qid"])
        for row in r["result"]["rows"]:
            print("  ", row)


if __name__ == "__main__":
    main()
