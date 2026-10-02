"""NON-GEO world join over pre-synchronized facts. An uploaded table of a non-geo type (hospital/...)
joins its faithful world table, filtered by country, aggregating the uploaded metric; entities not in
world.words abstain (serving never fetches or writes shared facts). Live world Postgres.

  Needs a synced world Postgres (docker-compose + db/sync) and KB_PG_* env vars set.
  python -m tests.test_nongeo
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal

HOSP = {"name": "hospitals", "columns": ["hospital", "beds"], "rows": [
    ["Massachusetts General Hospital", 100], ["Cleveland Clinic", 80],
    ["Johns Hopkins Hospital", 60], ["Charite", 50]]}              # Charite = Berlin/Germany -> excluded from US
# The demo sheets (web/public/dataset/formfacade-bank-deposits, neartail-catering,
# formesign-hospital-transfers). Countries: UBS and Credit Suisse Switzerland, Deutsche Bank Germany, BNP
# Paribas France, JPMorgan Chase United States, Barclays United Kingdom; the French Laundry and Eleven Madison
# Park United States; Toronto General Canada, Charite Germany, the other four hospitals United States.
BANKS = {"name": "deposits", "columns": ["bank", "account manager", "deposits"], "rows": [
    ["UBS", "Martin Keller", 900], ["Credit Suisse", "Anna Roth", 650], ["Deutsche Bank", "Jonas Weber", 700],
    ["BNP Paribas", "Claire Dubois", 550], ["JPMorgan Chase", "Emily Park", 1200],
    ["Barclays", "Oliver Grant", 500]]}
CATERING = {"name": "catering", "columns": ["restaurant", "event", "amount"], "rows": [
    ["The French Laundry", "Wine Dinner", 5200], ["Noma", "Chef Residency", 4800],
    ["Osteria Francescana", "Truffle Gala", 3900], ["Eleven Madison Park", "Private Tasting", 4400],
    ["The Fat Duck", "Anniversary Menu", 3600], ["El Celler de Can Roca", "Harvest Feast", 4100]]}
TRANSFERS = {"name": "transfers", "columns": ["hospital", "signed", "transfers"], "rows": [
    ["Mayo Clinic", "2026-08-04", 14], ["Massachusetts General Hospital", "2026-08-06", 11],
    ["Johns Hopkins Hospital", "2026-08-08", 9], ["Cleveland Clinic", "2026-08-11", 12],
    ["Charite", "2026-08-13", 7], ["Toronto General Hospital", "2026-08-15", 8]]}


def _scalar(res):
    rows = (res or {}).get("result", {}).get("rows") or []
    if rows and rows[0]:
        try:
            return int(float(str(rows[0][0]).replace(",", "")))
        except (ValueError, TypeError):
            return rows[0][0]
    return None


def main():
    if not os.environ.get("KB_PG_PASSWORD"):
        print("set KB_PG_PASSWORD"); return 1
    from engine.knowledge_query import KnowledgeQuery
    Q = KnowledgeQuery()
    from regress.live_schema import live_schema, served
    schema = live_schema().name
    fails = []
    # -ies PLURALS must name the type (regression: the question gate matched only "<type>s?", so
    # "universities" never matched "university" and the whole family fell to the clarify path even
    # though the cells grounded). Positive UK + contrastive US on the same table.
    UNI = {"name": "applications", "columns": ["university", "applicants"], "rows": [
        ["Arts University Plymouth", 90], ["Bath Spa University", 60],          # Q145 (UK)
        ["Adelphi University", 120], ["Adams State University", 80]]}           # Q30 (US)
    for country, want in (("United Kingdom", 150), ("United States", 200)):
        ru = served(schema, Q.serve, [UNI], f"total applicants for universities in {country}", schema=schema)
        gu = _scalar(ru)
        print(f"applicants, {country} universities -> {gu} (exp {want})  model={(ru or {}).get('model','')[:46]}")
        if gu != want:
            fails.append(f"plural 'universities' {country} != {want} (got {gu})")

    # SUM the uploaded metric over US hospitals (every entity pre-synchronized in words)
    r1 = served(schema, Q.serve, [HOSP], "total beds for hospitals in United States", schema=schema)
    got1 = _scalar(r1)
    print(f"total beds, US hospitals -> {got1} (exp 240)  model={r1.get('model','')[:46]}")
    if got1 != 240:
        fails.append(f"SUM beds US != 240 (got {got1})")
    # The derivation trail (docs/SHEETS_AS_REASONING.md): lookup -> filtered -> total, each sheet's SQL
    # the EXECUTED statement (this path once shipped `resolve(...)` pseudo-SQL and no visible filter),
    # country values displayed as labels, and the filter dropping the non-US rows visibly.
    trail = [(v.get("op"), v.get("logical_name")) for v in r1.get("views") or []]
    if trail != [("world_join", "enriched_1"), ("filter", "filtered"), ("group_agg", "total")]:
        fails.append(f"non-geo trail wrong: {trail}")
    v_lookup, v_filtered = (r1.get("views") or [{}, {}])[0], (r1.get("views") or [{}, {}])[1]
    if not any(column.endswith("__country") for column in v_lookup.get("columns") or []):
        fails.append(f"lookup sheet missing the country column the filter uses: {v_lookup.get('columns')}")
    if not any(str(r[-1]) == "United States" for r in v_lookup.get("rows") or []):
        fails.append(f"lookup country shows qids, not labels: {[(r or [None])[-1] for r in v_lookup.get('rows') or []]}")
    if len(v_filtered.get("rows") or []) >= len(v_lookup.get("rows") or [None]):
        fails.append("filtered sheet drops nothing — the country filter is invisible again")
    if "resolve(" in (r1.get("sql") or "") or any("resolve(" in (v.get("sql") or "") for v in r1.get("views") or []):
        fails.append("illustrative pseudo-SQL (resolve(...)) returned — sheets must show executed SQL only")
    if "columns" not in (r1.get("result") or {}):                  # the client render reads result.columns (NOT .cols);
        fails.append("result missing 'columns' key — the UI table would render empty")  # the value alone isn't enough
    if r1.get("unmatched") or r1.get("warnings"):
        fails.append(f"every hospital matched, yet the answer reports unmatched rows: {r1.get('unmatched')}")
    # A name no hospital matches is said, never dropped without a word (2026-10-02): the total stays the
    # matched hospitals', and the row is reported to the reply and the rail. When such rows are most of the
    # rows the answer could count, it declines instead.
    unknown = {"name": "hospitals", "columns": ["hospital", "beds"], "rows": [*HOSP["rows"], ["Xqzv Kpltr", 40]]}
    r_unknown = served(schema, Q.serve, [unknown], "total beds for hospitals in United States", schema=schema)
    disclosed = r_unknown.get("unmatched") or {}
    print(f"total beds, one unmatched -> {_scalar(r_unknown)} unmatched={disclosed.get('rows')}/{disclosed.get('of')}")
    if (_scalar(r_unknown) != 240
            or (disclosed.get("rows"), disclosed.get("of"), disclosed.get("names")) != (1, 5, ["Xqzv Kpltr"])
            or not any("Xqzv Kpltr" in warning for warning in r_unknown.get("warnings") or [])):
        fails.append(f"an unmatched hospital was not disclosed: total={_scalar(r_unknown)} unmatched={disclosed} "
                     f"warnings={r_unknown.get('warnings')}")
    mostly = {"name": "hospitals", "columns": ["hospital", "beds"], "rows": [
        ["Cleveland Clinic", 80], ["Johns Hopkins Hospital", 60], ["Xqzv Kpltr", 10], ["Xqzv Kpltr", 20],
        ["Xqzv Kpltr", 30]]}
    r_mostly = served(schema, Q.serve, [mostly], "total beds for hospitals in United States", schema=schema)
    print(f"total beds, most unmatched -> clarify={r_mostly.get('clarify')} reason={r_mostly.get('reason')}")
    if (not r_mostly.get("clarify") or (r_mostly.get("unmatched") or {}).get("rows") != 3
            or (r_mostly.get("result") or {}).get("rows")):
        fails.append(f"most hospitals unmatched, yet answered: {r_mostly.get('result')} ({r_mostly.get('reason')})")
    # COUNT US hospitals
    r2 = served(schema, Q.serve, [HOSP], "how many hospitals in United States", schema=schema)
    got2 = _scalar(r2)
    print(f"count US hospitals       -> {got2} (exp 3)")
    if got2 != 3:
        fails.append(f"COUNT US hospitals != 3 (got {got2})")
    exact_table = {"name": "hospital_fees", "columns": ["hospital", "commission"], "rows": [
        ["Massachusetts General Hospital", "9007199254740993.1"],
        ["Cleveland Clinic", "0.1"], ["Johns Hopkins Hospital", "0.1"],
        ["Charite", "9999999999999999.9"],
    ]}
    exact_result = served(schema, Q.serve,
        [exact_table], "total commission for hospitals in United States", schema=schema,
    )
    exact_rows = (exact_result or {}).get("result", {}).get("rows") or []
    exact_value = exact_rows[0][0] if exact_rows and exact_rows[0] else None
    print(f"exact commission, US     -> {exact_value} (exp 9007199254740993.3)")
    try:
        exact_ok = Decimal(str(exact_value)) == Decimal("9007199254740993.3")
    except Exception:  # noqa: BLE001
        exact_ok = False
    if not exact_ok or not isinstance(exact_value, str):
        fails.append(f"exact non-geo SUM rounded or used unsafe wire type (got {exact_value!r})")

    # Natural-language follow-up rewrites may add a predicate connective such as "named". The
    # typed planner already binds the exact uploaded value; coverage must not discard that valid
    # result merely because the connective is not represented in SQL.
    named_table = {"name": "transfers", "columns": ["hospital", "transfers"], "rows": [
        ["Mayo Clinic", 14], ["Massachusetts General Hospital", 11],
    ]}
    named_result = served(schema, Q.serve,
        [named_table], "total transfers for hospitals named Mayo Clinic", schema=schema,
    )
    named_rows = (named_result or {}).get("result", {}).get("rows") or []
    named_value = named_rows[0][-1] if named_rows and named_rows[0] else None
    try:
        named_ok = Decimal(str(named_value)) == Decimal("14")
    except Exception:  # noqa: BLE001
        named_ok = False
    if named_result.get("clarify") or not named_ok:
        fails.append(f"named entity predicate was incorrectly clarified (got {named_result!r})")

    # World dimensions of a non-geo entity (Chrome exploration, 2026-10-01/02): the entity's country as the
    # dimension of a grouped or ranked total, its country's continent as a filter, and the uploaded rows
    # ranked inside that filter. Each was declined; "how many transfers in Canada" read Canada as a
    # hospital name. Expected values are derived from the sheets above.
    def rows_of(response):
        return [[str(cell) for cell in row] for row in (response.get("result") or {}).get("rows") or []]

    for tables, question, expected, ops in (
        ([BANKS], "which country has the most deposits?", [["Switzerland", "1550"]],
         ["world_join", "group_agg", "topn"]),
        ([BANKS], "which country has the fewest deposits?", [["United Kingdom", "500"]],
         ["world_join", "group_agg", "topn"]),
        ([BANKS], "which country has the most banks?", [["Switzerland", "2"]], ["world_join", "group_agg", "topn"]),
        ([BANKS], "which bank has the most deposits in Europe?", [["UBS", "900"]],
         ["world_join", "world_join", "filter", "group_agg", "topn"]),
        ([BANKS], "total deposits for banks in Europe", [["3300"]],
         ["world_join", "world_join", "filter", "group_agg"]),
        ([CATERING], "which country spent the most on catering?", [["United States", "9600"]],
         ["world_join", "group_agg", "topn"]),
        ([TRANSFERS], "which country has the most transfers?", [["United States", "46"]],
         ["world_join", "group_agg", "topn"]),
        ([TRANSFERS], "how many transfers in Canada?", [["8"]], ["world_join", "filter", "group_agg"]),
        # Contrastive, same sheet: the hospitals are counted when the question counts hospitals.
        ([TRANSFERS], "how many hospitals are in Canada?", [["1"]], ["world_join", "filter", "group_agg"]),
    ):
        response = served(schema, Q.serve, tables, question, schema=schema)
        got = rows_of(response)
        trail = [view.get("op") for view in response.get("views") or []]
        print(f"{question:48s} -> {got} trail={trail}")
        if got != expected or trail != ops or response.get("clarify") or response.get("error"):
            fails.append(f"{question!r}: expected {expected} over {ops}, got {got} over {trail} "
                         f"(clarify={response.get('clarify')}, error={response.get('error')})")
    by_country = served(schema, Q.serve, [BANKS], "total deposits by country", schema=schema)
    grouped = sorted(rows_of(by_country))
    print(f"total deposits by country -> {grouped}")
    if grouped != [["France", "550"], ["Germany", "700"], ["Switzerland", "1550"], ["United Kingdom", "500"],
                   ["United States", "1200"]]:
        fails.append(f"total deposits by country: got {grouped}")
    # The grain the total is computed per is recorded in the calculation evidence.
    grain = [branch.get("grouping") for branch in (by_country.get("computation") or {}).get("branches") or []]
    if grain != [[{"table": "bank", "column": "country"}]]:
        fails.append(f"total deposits by country: the grain is not recorded: {grain}")
    # Negative: no bank is in Asia, which is said, not answered with a blank or a zero.
    asia = served(schema, Q.serve, [BANKS], "total deposits for banks in Asia", schema=schema)
    if not asia.get("clarify") or rows_of(asia):
        fails.append(f"total deposits for banks in Asia answered {rows_of(asia)} instead of saying none matched")
    # Negative: a country column of the upload answers "which country" as own data, not the bank's country.
    branches = {"name": "branches", "columns": ["bank", "country", "deposits"], "rows": [
        ["UBS", "Germany", 900], ["Credit Suisse", "Switzerland", 650], ["Deutsche Bank", "Germany", 700]]}
    own = served(schema, Q.serve, [branches], "which country has the most deposits?", schema=schema)
    print(f"own country column -> {rows_of(own)} model={(own.get('model') or '')[:40]}")
    if "non-geo" in (own.get("model") or "") or not rows_of(own) or rows_of(own)[0][0] != "Germany":
        fails.append(f"an uploaded country column lost to the bank's world country: {rows_of(own)} "
                     f"model={own.get('model')}")

    print("\n" + ("PASS — non-geo world join over pre-synchronized facts works" if not fails
                  else "FAIL:\n  " + "\n  ".join(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
