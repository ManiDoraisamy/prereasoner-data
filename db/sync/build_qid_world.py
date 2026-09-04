"""Build the QID-keyed city and country serving projections offline.

The request path resolves uploaded values through ``knowledgebase.words`` and joins
these projections by QID. Both tables are derived entirely from the synchronized
``public.settlement`` and ``public.country`` staging tables; serving never contacts
Wikidata or mutates shared reference data.
"""
from __future__ import annotations

from db.sync._conn import connect
from db.sync.schedule import ensure_catalog, record_refresh


CITY_DDL = """
CREATE TABLE IF NOT EXISTS knowledgebase."city" (
  qid text PRIMARY KEY,
  name text,
  country text,
  population bigint,
  updated_at date,
  source text
)
"""

COUNTRY_DDL = """
CREATE TABLE IF NOT EXISTS knowledgebase."country" (
  qid text PRIMARY KEY,
  name text,
  continent text,
  currency text,
  capital text,
  population bigint,
  updated_at date,
  source text
)
"""

_CITY_COLUMNS = {
    "name": "text",
    "country": "text",
    "population": "bigint",
    "updated_at": "date",
    "source": "text",
}
_COUNTRY_COLUMNS = {
    "name": "text",
    "continent": "text",
    "currency": "text",
    "capital": "text",
    "population": "bigint",
    "updated_at": "date",
    "source": "text",
}


def _ensure_columns(cursor, table: str, columns: dict[str, str]) -> None:
    """Upgrade legacy discovered tables without replacing their extra columns."""
    for column, sql_type in columns.items():
        cursor.execute(
            f'ALTER TABLE knowledgebase."{table}" '
            f'ADD COLUMN IF NOT EXISTS "{column}" {sql_type}'
        )


def rebuild(connection) -> dict[str, int]:
    """Atomically replace both derived projections and record their refresh."""
    cursor = connection.cursor()
    try:
        cursor.execute(CITY_DDL)
        cursor.execute(COUNTRY_DDL)
        _ensure_columns(cursor, "city", _CITY_COLUMNS)
        _ensure_columns(cursor, "country", _COUNTRY_COLUMNS)
        cursor.execute('TRUNCATE knowledgebase."city", knowledgebase."country"')
        cursor.execute(
            """
            INSERT INTO knowledgebase."country"
              (qid, name, continent, currency, capital, population, updated_at, source)
            SELECT qid, name, continent_qid, currency_code, capital_qid, population,
                   CURRENT_DATE, 'Wikidata synchronized projection'
              FROM public.country
             WHERE qid IS NOT NULL AND name IS NOT NULL
            """
        )
        country_count = cursor.rowcount
        cursor.execute(
            """
            INSERT INTO knowledgebase."city"
              (qid, name, country, population, updated_at, source)
            SELECT qid, name, country_qid, population, CURRENT_DATE,
                   'Wikidata synchronized projection'
              FROM public.settlement
             WHERE qid IS NOT NULL AND name IS NOT NULL
            """
        )
        city_count = cursor.rowcount
        ensure_catalog(cursor)
        record_refresh(cursor, "country")
        record_refresh(cursor, "city")
        connection.commit()
        return {"city": city_count, "country": country_count}
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()


def main() -> None:
    connection = connect()
    try:
        counts = rebuild(connection)
    finally:
        connection.close()
    print(
        "QID world projections rebuilt: "
        f"city={counts['city']}, country={counts['country']}",
        flush=True,
    )


if __name__ == "__main__":
    main()
