"""WorldReasoner = ComposedWorldQuery (composition + world + aggregates) PLUS a geo NEARBY primitive (lat/lng
distance search), ADDITIVELY (it wraps, never modifies, the composed planner — no regression risk).

A "near/around/closest <city>" question resolves the reference city to its lat/lng (public.settlement, the
clean ~174k-row geo source) and returns the nearest world cities by haversine distance. Everything else
delegates to ComposedWorldQuery unchanged (population ranking, aggregates, hybrid, clarify, composition).
"""
from __future__ import annotations
import re

from engine.world_compose import ComposedWorldQuery
from engine.pg import _pg

NEAR = re.compile(r"\b(near(?:est|by)?|closest|around|close to)\b", re.I)
STOP = {"cities", "city", "towns", "town", "places", "show", "find", "list", "me", "the", "biggest", "big",
        "largest", "major", "to", "of", "in", "which", "what", "are", "is", "by", "with", "and"}
HAVERSINE = ("6371*acos(greatest(-1,least(1, cos(radians(%s))*cos(radians(p.lat))*cos(radians(p.lng)-radians(%s))"
             "+sin(radians(%s))*sin(radians(p.lat)))))")


class WorldReasoner:
    def __init__(self):
        self.composed = ComposedWorldQuery()
        self.qw = self.composed.qw                                              # expose WorldQuery (server warmup: MODEL.qw._spacy())

    def serve(self, tables, question, sub, as_of=None, emit=None):
        if NEAR.search(question or ""):
            r = self._nearby(question)
            if r:
                return r                                                    # geo nearby handled (server emits its result)
        # COVERAGE PRE-GATE: a question with no data-intent, no schema mention, and no resolvable entity is
        # conversational ("how does this work?"), not a query. Short-circuit BEFORE reasoning (nothing garbage
        # streams) with low_confidence -> the UI answers it in-chat via the Sonnet fallback. Best-effort.
        try:
            if not self.composed._has_data_signal(question, tables):
                return {"question": question, "as_of": as_of, "low_confidence": True,
                        "clarify": None, "error": None, "result": None,
                        "model": "engine - conversational (not a data query)"}
        except Exception as e:                                              # noqa: BLE001 — never block a real query
            print("coverage pre-gate skipped:", e, flush=True)
        return self.composed.serve(tables, question, sub, as_of=as_of, emit=emit)  # else delegate (no regression)

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
                        "AND lat IS NOT NULL ORDER BY population DESC NULLS LAST LIMIT 1", (ref,))
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
            sql = (f'SELECT p.name, p.country, p.population, round(({HAVERSINE})::numeric,0) AS km '
                   f'FROM public.settlement p WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL '
                   f'AND p.population > %s AND lower(p.name) <> lower(%s) '
                   f"AND p.name IS NOT NULL AND p.name !~ '^Q[0-9]+$' "
                   f"AND p.name !~* 'arrondissement' "
                   f'AND (p.admin_qid IS NULL OR p.admin_qid <> %s) '
                   f'ORDER BY km ASC LIMIT %s')
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
    if not os.environ.get("WORLD_PG_PASSWORD"):
        print("set WORLD_PG_PASSWORD"); return
    q = WorldReasoner()
    r = q._nearby("big cities near Paris")
    if r:
        print("ref:", r["reference"]["name"], r["reference"]["qid"])
        for row in r["result"]["rows"]:
            print("  ", row)


if __name__ == "__main__":
    main()
