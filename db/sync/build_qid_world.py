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

# Serving resolves an uploaded place by its lowercased NAME (engine/pg.py:ambiguities, the world joins):
# without these a lookup is a sequential scan of ~200k cities, ~150 ms each, once per distinct value.
NAME_INDEXES = (
    'CREATE INDEX IF NOT EXISTS ix_kb_city_lower_name ON knowledgebase."city" (lower(name))',
    'CREATE INDEX IF NOT EXISTS ix_kb_country_lower_name ON knowledgebase."country" (lower(name))',
)

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
    """Bring a legacy discovered table to the declared columns and types, keeping its extra columns.

    Adding a missing column never changes an existing one, so a table pre-created with all-TEXT property
    columns kept population as text through every rebuild, and "What is the total population?" summed
    text (2026-09-28). The rebuild calls this on the emptied tables, so a type change converts no rows."""
    cursor.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'knowledgebase' AND table_name = %s",
        (table,),
    )
    existing = dict(cursor.fetchall())
    for column, sql_type in columns.items():
        if column not in existing:
            cursor.execute(
                f'ALTER TABLE knowledgebase."{table}" ADD COLUMN "{column}" {sql_type}'
            )
        elif existing[column] != sql_type:
            cursor.execute(
                f'ALTER TABLE knowledgebase."{table}" ALTER COLUMN "{column}" '
                f'TYPE {sql_type} USING "{column}"::{sql_type}'
            )


def rebuild(connection) -> dict[str, int]:
    """Atomically replace both derived projections and record their refresh."""
    cursor = connection.cursor()
    try:
        cursor.execute(CITY_DDL)
        cursor.execute(COUNTRY_DDL)
        cursor.execute('TRUNCATE knowledgebase."city", knowledgebase."country"')
        _ensure_columns(cursor, "city", _CITY_COLUMNS)
        _ensure_columns(cursor, "country", _COUNTRY_COLUMNS)
        for statement in NAME_INDEXES:
            cursor.execute(statement)
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
