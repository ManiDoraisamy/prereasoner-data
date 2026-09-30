"""Question families over the shipped datasets. Live world Postgres.

tests.test_datasets checks each dataset's own prompt and follow-ups. This suite asks the same data the
families of questions whose failures reached production on 2026-09-29/30: a currency named as the
output unit also filtering the rows ("total amount in Europe in GBP" answered 810 from the GBP rows
alone), an ISO code read as a country ("how many orders in GBP" counted Guinea-Bissau's orders), a
place's type noun read as the answer ("total amount for all European countries in GBP"), a multi-word
place checked word by word ("the United Kingdom"), the sheet's own name read as a column ("GBP orders"
grouped by `ordered`), a learned ranking with no ranking word ("... in North America in USD" served
the top 3 customers), and a measure named in two words ("total weight kg for deliveries in Germany" counted
the deliveries).

Expected values are derived from the CSVs, never from the engine: row sums, a fixed city -> country ->
continent map, and the stored ECB rate for each row (the fact table's own date column, else the
response's as-of date) read straight from knowledgebase.exchange_rate, so an FX answer is checked to
0.5%, not to the ECB-drift tolerance of the demo golds.

  Needs a synced world Postgres (docker-compose + db/sync) and KB_PG_* env vars set.
  python -m tests.test_question_families
"""
from __future__ import annotations

import csv
import datetime
import io
import os
import sys
from pathlib import Path

DATASET_DIR = Path(__file__).resolve().parents[1] / "web" / "public" / "dataset"
PLACES = {  # city -> (country, continent); country -> continent for sheets keyed by country
    "London": ("United Kingdom", "Europe"), "Brussels": ("Belgium", "Europe"), "Paris": ("France", "Europe"),
    "Burbank": ("United States", "North America"), "Toledo": ("United States", "North America"),
    "Cleveland": ("United States", "North America"), "Kolkata": ("India", "Asia"),
    "Berlin": ("Germany", "Europe"), "Cairo": ("Egypt", "Africa"), "Madrid": ("Spain", "Europe"),
    "Hanoi": ("Vietnam", "Asia"), "Toronto": ("Canada", "North America"),
    "Chicago": ("United States", "North America"), "Vienna": ("Austria", "Europe"),
    "Karachi": ("Pakistan", "Asia"), "Warsaw": ("Poland", "Europe"),
    "Albuquerque": ("United States", "North America"),
}
CONTINENT = {"Germany": "Europe", "Japan": "Asia", "United States": "North America", "India": "Asia",
             "South Korea": "Asia", "Brazil": "South America", "United Kingdom": "Europe", "China": "Asia"}


def _rows(dataset, sheet):
    text = (DATASET_DIR / dataset / f"{sheet}.csv").read_text(encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _orders():
    return _rows("customer-orders", "orders")


def _where(rows, **tests):
    return [row for row in rows if all(test(row) for test in tests.values())]


def _in(place):
    return lambda row: place in (row["city"], *PLACES[row["city"]])


def cases():
    """(dataset, family, question, expected) — expected is a number, or ("fx", rows, measure, target,
    date_column) for a converted total, computed when the response's as-of date is known."""
    orders, contracts = _orders(), _rows("formesign-contracts", "contracts")
    shipping = _rows("neartail-shipping", "deliveries")
    total = lambda rows, column="amount": float(sum(float(row[column]) for row in rows))  # noqa: E731
    fx = lambda rows, target, measure="amount", date=None: ("fx", rows, measure, target, date)  # noqa: E731
    out = []
    for place, target, unit in (("Europe", "GBP", "GBP"), ("Europe", "GBP", "British pounds"),
                                ("Belgium", "USD", "USD"), ("the United Kingdom", "GBP", "GBP"),
                                ("the United States", "EUR", "euros"), ("North America", "USD", "USD"),
                                ("Asia", "EUR", "EUR"), ("London", "EUR", "EUR")):
        rows = _where(orders, place=_in(place.replace("the ", "")))
        out.append(("customer-orders", "unit is not a row filter", f"total amount in {place} in {unit}",
                    fx(rows, target)))
    for target in ("GBP", "EUR"):
        out.append(("customer-orders", "unit is not a row filter", f"total amount in {target}",
                    fx(orders, target)))
    for continent, demonym in (("Europe", "European"), ("North America", "North American")):
        rows = _where(orders, place=_in(continent))
        out.append(("customer-orders", "place noun is not the answer",
                    f"total amount for all {demonym} countries in GBP", fx(rows, "GBP")))
        out.append(("customer-orders", "place noun is not the answer",
                    f"how many orders from {demonym} countries", float(len(rows))))
    for code in ("GBP", "EUR", "INR"):
        out.append(("customer-orders", "a code in the upload is not a country", f"how many orders in {code}",
                    float(len(_where(orders, code=lambda row, c=code: row["currency"] == c)))))
    for code, target in (("GBP", "USD"), ("EUR", "USD")):
        rows = _where(orders, code=lambda row, c=code: row["currency"] == c)
        out.append(("customer-orders", "source filter and unit", f"total amount of {code} orders in {target}",
                    fx(rows, target)))
    for code, place in (("GBP", "Europe"), ("USD", "North America")):
        rows = _where(orders, code=lambda row, c=code: row["currency"] == c, place=_in(place))
        out.append(("customer-orders", "the sheet's name is not a column",
                    f"total amount of {code} orders in {place}", total(rows)))
    out.append(("customer-orders", "the sheet's name is not a column", "how many orders in North America",
                float(len(_where(orders, place=_in("North America"))))))
    for continent, target in (("Europe", "USD"), ("Asia", "EUR")):
        rows = [row for row in contracts if CONTINENT[row["country"]] == continent]
        out.append(("formesign-contracts", "unit is not a row filter",
                    f"total value for contracts in {continent} in {target}", fx(rows, target, "value", "signed")))
    for code in ("EUR", "JPY"):
        out.append(("formesign-contracts", "a code in the upload is not a country", f"how many contracts in {code}",
                    float(sum(1 for row in contracts if row["currency"] == code))))
    # Toronto makes North America differ from the United States here; on customer-orders every North
    # American order is from the US, so a question read as the country scored right by accident.
    for continent, demonym in (("Europe", "European"), ("Asia", "Asian"), ("North America", "North American")):
        rows = [row for row in shipping if PLACES[row["city"]][1] == continent]
        out.append(("neartail-shipping", "place noun is not the answer",
                    f"total fee for deliveries to {demonym} cities", total(rows, "fee")))
        out.append(("neartail-shipping", "place", f"how many deliveries in {continent}", float(len(rows))))
    for place in ("Germany", "the United States"):
        rows = [row for row in shipping if PLACES[row["city"]][0] == place.replace("the ", "")]
        out.append(("neartail-shipping", "a two-word measure is not a row count",
                    f"total weight kg for deliveries in {place}", total(rows, "weight kg")))
    return out


def _rate(connection, code, target, day):
    cursor = connection.cursor()
    cursor.execute(f'SELECT rate_to_{target.lower()} FROM knowledgebase.exchange_rate '
                   "WHERE currency_code = %s AND date = %s", (code, day))
    row = cursor.fetchone()
    connection.rollback()
    if row is None or row[0] is None:
        raise LookupError(f"no {code}->{target} rate for {day}")
    return float(row[0])


def expected_value(expected, response, connection):
    if not isinstance(expected, tuple):
        return expected, 0.011
    _fx, rows, measure, target, date_column = expected
    as_of = str(response.get("as_of") or datetime.datetime.now(datetime.timezone.utc).date())[:10]
    value = sum(float(row[measure]) * _rate(connection, row["currency"], target,
                                            row[date_column] if date_column else as_of) for row in rows)
    return value, abs(value) * 0.005


def main() -> int:
    if not os.environ.get("KB_PG_PASSWORD"):
        print("set KB_PG_PASSWORD")
        return 1
    from engine import request_timing
    from engine.knowledge import KnowledgeReasoner
    from engine.pg import _pg
    from regress.live_schema import live_schema, served
    from tests.test_datasets import _tables

    reasoner = KnowledgeReasoner()
    schema = live_schema().name
    connection = _pg()
    tables, failures, plan = {}, [], cases()
    try:
        for dataset, family, question, expected in plan:
            tables.setdefault(dataset, _tables(DATASET_DIR / dataset))
            token = request_timing.begin("families")
            try:
                response = served(schema, reasoner.serve, tables[dataset], question, schema)
            finally:
                request_timing.end(token)
            rows = ((response.get("result") or {}).get("rows")) or []
            try:
                want, tolerance = expected_value(expected, response, connection)
                got = float(str(rows[0][0]).replace(",", "")) if len(rows) == 1 and len(rows[0]) == 1 else None
            except (LookupError, ValueError) as exc:
                want, tolerance, got = None, 0, None
                response = {**response, "error": response.get("error") or str(exc)}
            passed = got is not None and want is not None and abs(got - want) <= tolerance
            reason = (response.get("error") or ("clarify: " + str(response.get("reason") or response.get("model")))
                      if response.get("clarify") or response.get("error") else f"expected {want}, got {rows[:3]}")
            print(f"  {'ok  ' if passed else 'FAIL'} [{family}] {dataset}: {question!r}"
                  + ("" if passed else f" -> {reason}"), flush=True)
            if not passed:
                failures.append((dataset, question))
    finally:
        connection.close()
    print(f"\nquestion families: {len(plan) - len(failures)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
