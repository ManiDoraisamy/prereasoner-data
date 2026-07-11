"""GENERALIZED world-knowledge routing.

Same model + multi-tenant Postgres execution as engine.pg. The ONE change is `route()`: instead of city-only,
a column routes to ANY world table whose `concept` list matches its cell values' entity-class nouns
(Cities / Countries / States / Elements). This layer also registers the extra world-table configs
(`word_element`, `word_state`) on top of the base ones (`word_city`/`country`), and teaches the world-column
SELECT path about element properties (atomic_number / mass / symbol).

Why it honours the data: a corpus survey showed the world-joinable demand is geographic subdivisions
(american_state was the #2 concept) + chemical elements — everything else is already served (cities/countries)
or not joinable (people). So this adds exactly States + Elements.
"""
from __future__ import annotations
import json
import re

from engine import world_tables as _wt          # module — we extend its WORLD_COL_SYN in place
from engine.config import DATA_DIR
from engine.pg import PgQuery, _pg
from engine.world_tables import WORLD_NAMES, csv_table  # noqa: F401  (csv_table re-exported)

DATA = DATA_DIR

# friendly names for the extra world tables (mirrors WORLD_NAMES for the originals)
# word_state -> the qid-keyed world."u_s_state" (migrated; docs/notes/naming.md). element/continent remain
# on the friendly name-keyed family.
FRIENDLY15 = {"word_element": "Elements in the World", "word_state": "u_s_state",
              "word_continent": "Continents in the World"}
ALL_FRIENDLY = {**WORLD_NAMES, **FRIENDLY15}

# routing targets, in PRIORITY order (only matters for genuinely city-vs-X ambiguous cells).
ROUTE_ORDER = ["Cities in the World", "Countries in the World", "u_s_state", "Elements in the World"]

# SPECIFIC routing concepts per family — deliberately NOT the broad word_*.json `concept` lists, which include
# generic geographic terms (location/region/district/geographical_area) that overlap subdivisions and made a
# generic-HEADER column of state/country VALUES mis-route to Cities (the first family in ROUTE_ORDER). A cell
# routes to a family only when it fires one of that family's SPECIFIC concepts; the generic geographic terms are
# a Cities-only FALLBACK used only when NO specific family fired (so "California" [american_state, location]
# routes to States, not Cities via "location"). This makes routing header-INDEPENDENT, the stated objective.
ROUTE_CONCEPTS = {
    "Cities in the World": {"city", "municipality", "urban_area", "town", "capital", "national_capital",
                            "metropolis", "borough", "village", "port", "seaport", "commune"},
    "Countries in the World": {"country", "nation", "sovereign_state", "european_country", "asian_country",
                               "african_country", "north_american_country", "south_american_country",
                               "balkan_country", "arab_country"},
    "u_s_state": {"american_state", "australian_state", "italian_region", "state", "province",
                  "county", "prefecture", "department", "oblast", "canton", "governorate",
                  "territory", "federal_state", "autonomous_community", "u.s._state"},
    "Elements in the World": {"chemical_element", "element", "metallic_element", "metal", "halogen",
                              "noble_gas", "metalloid", "alkali_metal", "alkaline_earth_metal",
                              "transition_metal", "nonmetal", "rare_earth_element", "actinide", "lanthanide"},
}
GENERIC_CITY = {"location", "place", "region", "district", "geographical_area", "area",
                "geographic_point", "point", "settlement"}

# teach the world-column SELECT path (WorldTableQuery.world_target) about element + state properties, so a
# question can ask for them ("atomic number of …", "the symbol of …"). WORLD_COL_SYN is a process-global the
# planner reads; extending it here only affects the world service.
_wt.WORLD_COL_SYN.update({
    "atomic": "atomic_number", "atomic_number": "atomic_number",
    "mass": "mass", "weight": "mass", "symbol": "symbol",
})


class RoutedQuery(PgQuery):
    """PgQuery (model + Postgres) + generalized concept→world-table routing + States/Elements configs."""

    def __init__(self, deploy_dir):
        super().__init__(deploy_dir)
        for fp in sorted(DATA.glob("word_*.json")):
            d = json.loads(fp.read_text(encoding="utf-8"))
            d["table"] = FRIENDLY15.get(d["table"], d["table"])
            for link in d.get("links", []):
                link["to_table"] = ALL_FRIENDLY.get(link["to_table"], link["to_table"])
            self.words[d["table"]] = d         # register the extra world tables alongside the base ones
        self._caliases = None                  # lazy cache of the world "Country Aliases" normalization table

    @staticmethod
    def _world_aff(col):
        if col == "atomic_number":
            return "INTEGER"
        if col == "mass":
            return "REAL"
        return PgQuery._world_aff(col)

    def _country_alias_map(self):
        """Lazy-load the world."Country Aliases" NORMALIZATION TABLE (alias -> canonical country name = the world
        model's key), cached for the instance. Data-driven (canonical self-map + curated safe aliases), not a
        hardcoded dict. Falls back to {} (generic match only) if the table is absent."""
        if self._caliases is None:
            m = {}
            try:
                cn = _pg(); cur = cn.cursor()
                cur.execute('SELECT lower(alias), name FROM world."Country Aliases"')
                for a, n in cur.fetchall():
                    if a and n:
                        m[a] = n
                cn.close()
            except Exception:                       # noqa: BLE001
                pass
            self._caliases = m
        return self._caliases

    def _find_value(self, low_q, w):
        """Normalize a where-condition value via the world Country-Aliases table BEFORE the generic match — so
        "cities in US" resolves "us" -> the canonical PK "United States" (world join), instead of falling through
        to the own-data planner where the preposition "in" wrongly matched a 2-letter State code ('IN'). Longest
        alias first so "u.s.a." beats "us"."""
        if "country" in (w.get("filter_attrs") or []):
            amap = self._country_alias_map()
            if amap:
                cvals = {str(v).lower() for v in (w.get("filter_values", {}).get("country") or []) if v}
                for alias in sorted(amap, key=lambda a: -len(a)):
                    canon = amap[alias]
                    if canon.lower() in cvals and re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", low_q):
                        return "country", canon
        return super()._find_value(low_q, w)

    def _families(self):
        """[(specific_concepts, friendly_table)] for the routing targets present in self.words, in ROUTE_ORDER.
        Uses the SPECIFIC ROUTE_CONCEPTS (not the broad word_*.json lists), so generic geographic terms can't
        let Cities claim subdivision/country values."""
        return [(ROUTE_CONCEPTS[ft], ft) for ft in ROUTE_ORDER if ft in self.words and ft in ROUTE_CONCEPTS]

    def route(self, table):
        """A column routes to the world table whose SPECIFIC concepts its cell VALUES' entity-class nouns match
        (>=0.4, top 5). Header match wins; else the family with >=50% of non-empty cells. Generic geographic
        concepts (location/region/…) route to Cities ONLY as a fallback when no specific family fired — so a
        generic-HEADER column of state/country VALUES still routes correctly (header-independent)."""
        base = table["name"]
        fams = self._families()
        if not fams:
            return {}
        an = self.q11.analyze(table)
        cols = an["cols"]
        counts = {c: {ft: 0 for _, ft in fams} for c in cols}
        generic = {c: 0 for c in cols}
        nonempty = {c: 0 for c in cols}
        for row in an["rows"]:
            for ci, cell in enumerate(row["cells"]):
                evo = cell.get("evolution") or []
                if not evo:
                    continue
                col = cols[ci]
                nonempty[col] += 1
                top = [dn[4:] for dn, v in sorted(evo[-1].items(), key=lambda kv: -kv[1])
                       if dn.startswith("ace_") and v >= 0.4][:5]
                matched = False
                for cset, ft in fams:
                    if any(t in cset for t in top):
                        counts[col][ft] += 1                 # first SPECIFIC family in ROUTE_ORDER
                        matched = True
                        break
                if not matched and any(t in GENERIC_CITY for t in top):
                    generic[col] += 1                        # generic geographic -> Cities fallback only
        cities = "Cities in the World"
        routes = {}
        for c in cols:
            named = next((ft for cset, ft in fams if c.lower() in cset), None)
            if named:
                routes[(base, c)] = named                    # explicit header (city/state/country/element)
                continue
            n = nonempty[c]
            if n < 2:
                continue
            best_ft, best = max(counts[c].items(), key=lambda kv: kv[1])
            if best and best / n >= 0.5:
                routes[(base, c)] = best_ft                  # value-based: dominant specific family
            elif cities in counts[c] and (counts[c][cities] + generic[c]) / n >= 0.5:
                routes[(base, c)] = cities                   # specific-city + generic-geographic cells -> Cities
        return routes
