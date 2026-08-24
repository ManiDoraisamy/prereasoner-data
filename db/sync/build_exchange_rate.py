"""Populate knowledgebase."exchange_rate" — the joinable daily cross-rate world table — from ecb.exchange_rate.

The ECB source table (db/sync/sources/ecb) is the raw synced history: one row per
(business day, quote currency) with `units_per_eur`. This builds the world-table projection the
serving SQL joins, in the same family as knowledgebase."city"/"country"/"u_s_state":

  knowledgebase."exchange_rate"(currency_code, date, rate_to_<code>..., updated_at, source)

  - one row per (currency_code, CALENDAR day): weekends/holidays carry the previous business
    day's rates forward, so the serving join stays a plain equality on (code, date) — no
    temporal-window SQL at query time. `updated_at` keeps the SOURCE business date, so the
    existing freshness machinery reports "Saturday's rate is Friday's" honestly.
  - rate_to_<T>(c, d) = units_per_eur(T, d) / units_per_eur(c, d); EUR is the implicit base
    (units_per_eur = 1). A retired series (CYP, EEK, ...) is filled only to ITS OWN last
    published date — rates are never fabricated past the end of a series — and a rate_to_<T>
    column is NULL on dates where T itself has no series.
  - active codes (last date == the release's max date) extend to CURRENT_DATE so recent
    fact rows join; the gap still carries the last business date in updated_at.

Frequency is a property of the SYNC (daily for ECB), not of the join: the serving join is the
same conversation + tenant + knowledgebase shape used by every other world table.

Run AFTER db/sync/sources/ecb/sync.py:  python -m db.sync.build_exchange_rate
"""
from __future__ import annotations

import datetime

from psycopg2.extras import execute_values

try:
    from _conn import connect
except ImportError:
    from ._conn import connect

SOURCE = "ECB euro foreign exchange reference rates"
TABLE = 'knowledgebase."exchange_rate"'


def cross_rates(units_by_code: dict[str, float], codes: list[str]) -> dict[str, float | None]:
    """rate_to_<T> for one (source code, day): units(T)/units(source); None when T has no series."""
    source_units = units_by_code["__self__"]
    return {
        target: (units_by_code[target] / source_units if target in units_by_code else None)
        for target in codes
    }


def build_rows(history, today=None):
    """(code, date, {target: rate}) rows from raw (effective_date, quote_currency, units_per_eur).

    Pure so the hermetic test can drive it with a fixture. `history` is an iterable of
    (date, code, units) business-day observations; EUR is synthesized as the base.
    """
    by_day: dict[datetime.date, dict[str, float]] = {}
    for day, code, units in history:
        by_day.setdefault(day, {})[code] = float(units)
    if not by_day:
        return [], []
    days = sorted(by_day)
    first, last = days[0], days[-1]
    today = today or datetime.date.today()
    horizon = max(last, today)
    codes = sorted({code for units in by_day.values() for code in units} | {"EUR"})
    last_seen: dict[str, datetime.date] = {}
    for day in days:
        for code in by_day[day]:
            last_seen[code] = day
    for code in codes:
        if code == "EUR" or last_seen.get(code) == last:
            last_seen[code] = horizon                       # active series extend to today

    rows = []
    carried: dict[str, float] = {}
    carried_from: dict[str, datetime.date] = {}
    day = first
    while day <= horizon:
        observed = by_day.get(day, {})
        for code, units in observed.items():
            carried[code] = units
            carried_from[code] = day
        for code in codes:
            if code != "EUR" and (code not in carried or day > last_seen[code]):
                continue                                    # before first print, or a retired series
            units_by_code = {t: u for t, u in carried.items() if day <= last_seen[t]}
            units_by_code["EUR"] = 1.0
            units_by_code["__self__"] = 1.0 if code == "EUR" else carried[code]
            rows.append((code, day, cross_rates(units_by_code, codes),
                         carried_from.get(code, day) if code != "EUR" else max(
                             carried_from.values(), default=day)))
        day += datetime.timedelta(days=1)
    return rows, codes


DDL = """
CREATE TABLE IF NOT EXISTS {table} (
    currency_code text NOT NULL,
    "date"        date NOT NULL,
    {rate_columns},
    updated_at    date NOT NULL,
    source        text NOT NULL,
    PRIMARY KEY (currency_code, "date")
)
"""


def main() -> int:
    connection = connect()
    cur = connection.cursor()
    cur.execute("""SELECT effective_date, quote_currency, units_per_eur FROM ecb.exchange_rate
                   WHERE release_id = (SELECT MAX(release_id) FROM ecb.exchange_rate)
                   ORDER BY effective_date""")
    rows, codes = build_rows(cur.fetchall())
    rate_columns = ", ".join(f'rate_to_{code.lower()} double precision' for code in codes)
    cur.execute(DDL.format(table=TABLE, rate_columns=rate_columns))
    cur.execute(f"TRUNCATE {TABLE}")
    column_list = (["currency_code", '"date"']
                   + [f"rate_to_{code.lower()}" for code in codes] + ["updated_at", "source"])
    execute_values(
        cur,
        f'INSERT INTO {TABLE} ({", ".join(column_list)}) VALUES %s',
        [(code, day, *[rates.get(target) for target in codes], source_day, SOURCE)
         for code, day, rates, source_day in rows],
        page_size=2000,
    )
    connection.commit()
    cur.execute(f'SELECT COUNT(*), MIN("date"), MAX("date") FROM {TABLE}')
    count, lo, hi = cur.fetchone()
    print(f"knowledgebase.exchange_rate: {count} rows, {lo} .. {hi}, {len(codes)} codes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
