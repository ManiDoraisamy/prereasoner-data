"""The connected catalog (engine/program/connected.py) and the request's links that feed it (engine/prepared.py
RequestLinks, connected_workbook; engine/tables.py select_query). Hermetic: the request's database is a scratch one whose
bridge and knowledgebase tables the test writes itself, as production's world layer (engine/knowledge_bridges.py
request_links) holds them; engine.llm.DECISIONS_TRANSPORT is a fake and the stock reader's backbone a hashed fake
(tests/test_answer.py), so nothing reaches a network, a model file or Postgres.

usage: python -X utf8 -m tests.test_connected"""
from __future__ import annotations

import sys

from engine.prepared import (EntityLink, KeyLink, MoneyLink, PlaceLink, RequestLinks, connected_workbook, current_database, prepared_workbook,
                             scratch_database)
from engine.program.closed import NONE, value_set_scores
from engine.program.connected import ARROW, LINK_MIN_CELLS, Link, Upload, conversion_targets, linked_columns, links_on
from engine.program.store import RequestTables, sql_table, text_column
from engine.tables import TableQuery, normalize_tables
from tests.test_answer import FakeTransport, _option, _use_fake_reader, _with_transport

ORDERS = {"name": "orders", "columns": ["Customer", "City", "Amount", "Currency"],
          "rows": [["Ann", "Paris", "100", "EUR"], ["Bob", "Lyon", "50", "EUR"], ["Cy", "Tokyo", "2000", "JPY"],
                   ["Di", "Osaka", "3000", "JPY"], ["Ed", "Atlantis", "10", "EUR"]]}
CUSTOMERS = {"name": "customers", "columns": ["Customer", "Tier"],
             "rows": [["Ann", "Gold"], ["Bob", "Silver"], ["Cy", "Gold"], ["Di", "Bronze"], ["Ed", "Gold"]]}
BRIDGE = "orders connected to knowledgebase"
AS_OF = "2026-11-18"
PLACE = PlaceLink("orders", "City", 1, "city", BRIDGE, True, 5, 4)
KEY = KeyLink("orders", "Customer", 0, "customers", "Customer", 0)
MONEY = MoneyLink("orders", 3, None, (2,), ("rate_to_gbp", "rate_to_usd"), AS_OF)


def _links(place=PLACE, keys=(KEY,), money=(MONEY,), entities=()):
    return RequestLinks({"orders": tuple(ORDERS["columns"]), "customers": tuple(CUSTOMERS["columns"])}, (place,) if place else (), tuple(entities),
                        tuple(keys), tuple(money))


def _knowledgebase(database, tokyo=13_960_000):
    """The bridge and the knowledgebase rows production's world layer would hold for ORDERS' cities and currencies."""
    c = database.connection
    c.execute(f'CREATE TABLE main."{BRIDGE}" ("column" TEXT, "value" TEXT, "world_type" TEXT, "world_key" TEXT, "country" TEXT, '
              '"world_qid" TEXT, "entity_qid" TEXT, "context" TEXT)')
    c.executemany(f'INSERT INTO main."{BRIDGE}" VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                  [("City", "Paris", "city", "Q90", "France", "Q515", None, ""), ("City", "Lyon", "city", "Q456", "France", "Q515", None, ""),
                   ("City", "Tokyo", "city", "Q1490", "Japan", "Q515", None, ""), ("City", "Osaka", "city", "Q35765", "Japan", "Q515", None, "")])
    c.execute('CREATE TABLE knowledgebase."city" ("qid" TEXT, "country" TEXT, "population" REAL)')
    c.executemany('INSERT INTO knowledgebase."city" VALUES (?, ?, ?)',
                  [("Q90", "Q142", 2_100_000), ("Q456", "Q142", 520_000), ("Q1490", "Q17", tokyo), ("Q35765", "Q17", 2_750_000)])
    c.execute('CREATE TABLE knowledgebase."country" ("qid" TEXT, "name" TEXT, "continent" TEXT, "currency" TEXT, "population" REAL)')
    c.executemany('INSERT INTO knowledgebase."country" VALUES (?, ?, ?, ?, ?)',
                  [("Q142", "France", "Q46", "EUR", 68_000_000), ("Q17", "Japan", "Q48", "JPY", 124_000_000)])
    c.execute('CREATE TABLE knowledgebase."_labels" ("qid" TEXT, "label" TEXT)')
    c.executemany('INSERT INTO knowledgebase."_labels" VALUES (?, ?)', [("Q46", "Europe"), ("Q48", "Asia")])
    c.execute('CREATE TABLE knowledgebase."exchange_rate" ("currency_code" TEXT, "date" TEXT, "rate_to_usd" REAL, "rate_to_gbp" REAL)')
    c.executemany('INSERT INTO knowledgebase."exchange_rate" VALUES (?, ?, ?, ?)',
                  [("EUR", AS_OF, 1.1, 0.85), ("JPY", AS_OF, 0.0067, 0.0052), ("EUR", "2026-11-17", 1.0, 0.8)])
    c.commit()


def _tables(customers=CUSTOMERS):
    return normalize_tables([ORDERS, customers])


def _connected(target=None, customers=CUSTOMERS, links=None, tokyo=13_960_000):
    """(plain key, plain preparation, connected key, connected preparation) on a fresh scratch database."""
    _use_fake_reader()
    tables = _tables(customers)
    with scratch_database() as database:
        _knowledgebase(database, tokyo)
        key, plain = _with_transport(FakeTransport(lambda *a: {}), lambda: prepared_workbook(tables))
        connected_key, connected = connected_workbook(key, plain, links or _links(), database.connection, target)
        return key, plain, connected_key, connected


def _names(catalog, table_id="t0"):
    return [column.name for column in catalog.table(table_id).columns]


def _column(catalog, name, table_id="t0"):
    table = catalog.table(table_id)
    position = _names(catalog, table_id).index(name)
    return table.texts[position], table.numbers[position]


# ------------------------------------------------------------------------------------------------------- positive
def test_a_place_column_reaches_its_country_continent_currency_and_population() -> None:
    _key, plain, _connected_key, connected = _connected()
    assert _names(plain.catalog) == ["Customer", "City", "Amount", "Currency"], _names(plain.catalog)
    names = _names(connected.catalog)
    assert names[:8] == ["Customer", "City", "Amount", "Currency", f"City{ARROW}country", f"City{ARROW}population",
                         f"City{ARROW}country{ARROW}continent", f"City{ARROW}country{ARROW}currency"], names
    countries, _ = _column(connected.catalog, f"City{ARROW}country")
    continents, _ = _column(connected.catalog, f"City{ARROW}country{ARROW}continent")
    _, population = _column(connected.catalog, f"City{ARROW}population")
    assert countries == ["France", "France", "Japan", "Japan", None], countries     # a cell no bridge row matches reaches nothing
    assert continents == ["Europe", "Europe", "Asia", "Asia", None], continents
    assert population[2] == 13_960_000.0
    assert _names(plain.catalog) == ["Customer", "City", "Amount", "Currency"], "the workbook's own catalog is never changed"
    print("  PASS  a place column reaches its country, continent, currency and population")


def test_a_key_reaches_the_other_tables_columns() -> None:
    _key, _plain, _connected_key, connected = _connected()
    tiers, _ = _column(connected.catalog, f"Customer{ARROW}Tier")
    assert tiers == ["Gold", "Silver", "Gold", "Bronze", "Gold"], tiers
    assert connected.linked == frozenset({("t0.0", "t1.0"), ("t1.0", "t0.0")}), connected.linked
    print("  PASS  a key reaches the other table's columns, and its two columns are linked for the value sets")


def test_a_money_column_reads_in_the_target_currency_on_the_as_of_day() -> None:
    _key, _plain, _connected_key, connected = _connected("USD")
    names = _names(connected.catalog)
    assert f"Amount{ARROW}USD" in names and f"Amount{ARROW}GBP" not in names, names
    _, usd = _column(connected.catalog, f"Amount{ARROW}USD")
    assert [round(value, 4) for value in usd] == [110.0, 55.0, 13.4, 20.1, 11.0], usd
    column = connected.catalog.table("t0").columns[names.index(f"Amount{ARROW}USD")]
    assert column.unit == "USD", "a converted column states its currency, so a total over it is one figure"
    assert conversion_targets(_links()) == ["GBP", "USD"]
    print("  PASS  a money column reads in the target currency, at the rate of its row's currency on the as-of day")


def test_the_request_tables_hold_the_reached_columns() -> None:
    _use_fake_reader()
    tables = _tables()
    with scratch_database() as database:
        _knowledgebase(database)
        key, plain = _with_transport(FakeTransport(lambda *a: {}), lambda: prepared_workbook(tables))
        _key, connected = connected_workbook(key, plain, _links(), database.connection, "USD")
        store = RequestTables(connected.catalog, database.connection)
        store.ensure("t0")
        position = _names(connected.catalog).index(f"City{ARROW}country")
        rows = database.connection.execute(f'SELECT "{text_column(position)}" FROM "{sql_table("t0")}" ORDER BY "_line"').fetchall()
        assert [row[0] for row in rows] == ["France", "France", "Japan", "Japan", None], rows
        held = {row[0] for row in database.connection.execute("SELECT name FROM sqlite_temp_master").fetchall()}
        assert not held, f"reach leaves no temporary table behind: {held}"
    print("  PASS  the request's tables hold the reached columns, and reading them leaves nothing behind")


def test_a_linked_value_is_read_as_one_across_the_key_and_its_column() -> None:
    names = ["value:t0.0=ann", "value:t1.0=ann", "value:t0.1=paris"]
    sets = {"saa": 0.3, "sab": 0.3, "sac": 0.1, "none": 0.3}      # {t0.0=ann}, {t1.0=ann}, {t0.1=paris}, ...
    linked = linked_columns([Upload("t0", 0, "t1", 0)])
    marginal = value_set_scores({}, names, sets)
    joined = value_set_scores({}, names, sets, linked)
    assert joined["value:t0.0=ann"] > marginal["value:t0.0=ann"] and joined["value:t1.0=ann"] > marginal["value:t1.0=ann"], (marginal, joined)
    assert joined["value:t0.1=paris"] == marginal["value:t0.1=paris"]
    print("  PASS  a value the key and its column both hold is read as one value")


# ---------------------------------------------------------------------------------------------------- contrastive
def test_a_place_column_few_cells_match_is_not_linked() -> None:
    few = PlaceLink("orders", "City", 1, "city", BRIDGE, True, 5, LINK_MIN_CELLS - 1)
    share = PlaceLink("orders", "City", 1, "city", BRIDGE, True, 40, 6)
    _key, plain, _ck, _c = _connected(links=_links(place=few, keys=(), money=()))
    assert links_on(plain.catalog, _links(place=few, keys=(), money=())) == []
    assert links_on(plain.catalog, _links(place=share, keys=(), money=())) == [], "production routes only on most of a column's cells"
    assert links_on(plain.catalog, _links(keys=(), money=())) == [Link("t0", 1, "City", "city", BRIDGE)]
    print("  PASS  a place column whose bridge holds few of its cells is not linked")


def test_a_key_whose_values_repeat_adds_nothing() -> None:
    twice = {**CUSTOMERS, "rows": [*CUSTOMERS["rows"], ["Ann", "Bronze"]]}
    _key, _plain, _ck, connected = _connected(customers=twice)
    assert f"Customer{ARROW}Tier" not in _names(connected.catalog), "a join on a repeated key would repeat lines"
    assert f"City{ARROW}country" in _names(connected.catalog)
    print("  PASS  a key whose values repeat in the other table adds nothing")


def test_a_link_on_another_column_at_that_place_is_not_placed() -> None:
    moved = RequestLinks({"orders": ("Customer", "Town", "Amount", "Currency"), "customers": tuple(CUSTOMERS["columns"])}, (PLACE,))
    _key, plain, _ck, _c = _connected()
    assert links_on(plain.catalog, moved) == []
    print("  PASS  a link whose column is another column of the compiled table is not placed")


def test_a_table_with_a_place_column_takes_no_entity_link() -> None:
    entity = EntityLink("orders", "Customer", 0, "bank", "bank", BRIDGE, False, True, 5, True)
    _key, plain, _ck, _c = _connected()
    placed = links_on(plain.catalog, _links(keys=(), money=(), entities=(entity,)))
    assert placed == [Link("t0", 1, "City", "city", BRIDGE)], placed
    alone = links_on(plain.catalog, _links(place=None, keys=(), money=(), entities=(entity,)))
    assert alone == [Link("t0", 0, "Customer", "bank", BRIDGE, "bank")], alone
    print("  PASS  a table with a place column takes no entity link")


# ------------------------------------------------------------------------------------------------------- negative
def test_without_links_the_workbook_is_its_own() -> None:
    _use_fake_reader()
    tables = _tables()
    with scratch_database() as database:
        key, plain = _with_transport(FakeTransport(lambda *a: {}), lambda: prepared_workbook(tables))
        assert database.links(key, tables) is None, "a database with no linker connects nothing"
        empty = RequestLinks({"orders": tuple(ORDERS["columns"])})
        assert connected_workbook(key, plain, empty, database.connection) == (key, plain)
    print("  PASS  without links the workbook is its own")


def test_the_preparation_is_kept_by_what_the_links_reach() -> None:
    _key, _plain, first, prepared = _connected()
    _key, _plain, again, kept = _connected()
    _key, _plain, other, _ = _connected(tokyo=14_000_000)
    _key, _plain, converted, _ = _connected("USD")
    assert first == again and kept is prepared, "two requests reaching the same rows share one preparation"
    assert other != first, "a request reaching other knowledgebase rows prepares its own"
    assert converted != first
    print("  PASS  the connected preparation is kept by what its links reach")


# -------------------------------------------------------------------------------------------- through select_query
def test_select_query_reads_the_connected_workbook_and_asks_the_currency_first() -> None:
    _use_fake_reader()
    tables = _tables()
    states = []

    def rule(name, question, state):
        states.append((name, state))
        if name == "convert":
            return {_option(question, "USD"): 0.9}
        if name == "reading":
            return {next(option for option in question["criteria"] if option != NONE): 0.9}
        return {NONE: 0.9} if NONE in question["criteria"] else {}
    transport = FakeTransport(rule)
    import engine.prepared as prepared_module
    built = []
    held = prepared_module.connected_workbook

    def counted(*args, **kwargs):
        built.append(args[4] if len(args) > 4 else kwargs.get("target"))
        return held(*args, **kwargs)
    prepared_module.connected_workbook = counted
    try:
        with scratch_database() as database:
            _knowledgebase(database)
            database.link_with(lambda _tables: _links())
            planner = TableQuery.__new__(TableQuery)
            question = "total amount in US dollars by country"
            selection = _with_transport(transport, lambda: planner.select_query(question, tables))
            again = _with_transport(transport, lambda: planner.select_query(question, tables))
            assert current_database() is database
    finally:
        prepared_module.connected_workbook = held
    assert built == [None, "USD"], f"a request connects its workbook once per currency: {built}"
    assert again.pool == selection.pool
    assert selection.converts, "the convert question read the currency: the path's reading certifies a conversion"
    called = [name for name, _ in states]
    assert called.count("convert") == 1, "the convert question is asked once per request"
    assert called[0] == "convert", called
    headers = {column["header"] for name, state in states if name != "convert" for table in state.get("workbook", ())
               for column in table["columns"]}
    assert f"Amount{ARROW}USD" in headers and f"City{ARROW}country" in headers and f"Customer{ARROW}Tier" in headers, sorted(headers)
    convert_headers = {column["header"] for name, state in states if name == "convert" for table in state["workbook"] for column in table["columns"]}
    assert f"Amount{ARROW}USD" not in convert_headers and f"City{ARROW}country" in convert_headers
    assert selection.pool, "the connected workbook lists candidates"
    print("  PASS  select_query asks the currency first, once per request, and reads the connected workbook")


TESTS = [
    test_a_place_column_reaches_its_country_continent_currency_and_population,
    test_a_key_reaches_the_other_tables_columns,
    test_a_money_column_reads_in_the_target_currency_on_the_as_of_day,
    test_the_request_tables_hold_the_reached_columns,
    test_a_linked_value_is_read_as_one_across_the_key_and_its_column,
    test_a_place_column_few_cells_match_is_not_linked,
    test_a_key_whose_values_repeat_adds_nothing,
    test_a_link_on_another_column_at_that_place_is_not_placed,
    test_a_table_with_a_place_column_takes_no_entity_link,
    test_without_links_the_workbook_is_its_own,
    test_the_preparation_is_kept_by_what_the_links_reach,
    test_select_query_reads_the_connected_workbook_and_asks_the_currency_first,
]


def main() -> int:
    print("=== the connected catalog: a request's links on its workbook ===")
    failed = 0
    for test in TESTS:
        try:
            test()
        except Exception as error:      # noqa: BLE001 - report every test
            failed += 1
            print(f"  FAIL  {test.__name__}: {type(error).__name__}: {error}")
    print(f"test_connected: {len(TESTS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
