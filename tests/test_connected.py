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


def test_a_key_and_what_it_reaches_hold_the_other_tables_values() -> None:
    """The connected catalog records which column holds another's values (Catalog.identities): the key holds the
    column it keys into and each column it reaches holds the column it was read from, so a decomposition binds an
    anti-join key on either side by its lineage (engine/program/store.py column_lineage). A place a link reaches is
    the knowledgebase's, never a request table's: it holds no request column's values. The plain workbook has none."""
    from engine.program.store import column_lineage, fold_column, text_column

    _key, plain, _connected_key, connected = _connected()
    names = _names(connected.catalog)
    tier = names.index(f"Customer{ARROW}Tier")
    identities = connected.catalog.identities
    assert identities[("t0", 0)] == ("t1", 0), identities
    assert identities[("t0", tier)] == ("t1", 1), identities
    assert not any(key[1] == names.index(f"City{ARROW}country") for key in identities if key[0] == "t0"), identities
    assert plain.catalog.identities == {}
    lineage = column_lineage(connected.catalog)
    assert lineage[("_t0", fold_column(tier))] == ("_t1", fold_column(1))
    assert lineage[("_t0", text_column(0))] == ("_t1", text_column(0))


def test_an_answer_names_a_column_a_key_reaches_by_its_sheets_header() -> None:
    """complex-promotions' fused answer named its customers "order_id → customer_id → customer_name" where the CSVs and
    the demo gate name the column customer_name. An answer names a column the request's key links reach by the header
    of the sheet column whose values it holds (Catalog.identities); a column reached in the knowledgebase keeps its path,
    and a sheet's own column keeps its own header even where it holds another table's values. A listing of several
    columns names each."""
    from engine.program.language import Col, Show
    from engine.program.output import answer_columns, header

    clients = {"name": "customers", "columns": ["Name", "Tier"], "rows": CUSTOMERS["rows"]}
    links = RequestLinks({"orders": tuple(ORDERS["columns"]), "customers": tuple(clients["columns"])}, (PLACE,), (),
                         (KeyLink("orders", "Customer", 0, "customers", "Name", 0),), (MONEY,))
    _key, _plain, _connected_key, connected = _connected(customers=clients, links=links)
    catalog = connected.catalog
    names = _names(catalog)
    tier, country = names.index(f"Customer{ARROW}Tier"), names.index(f"City{ARROW}country")
    assert catalog.identities[("t0", 0)] == ("t1", 0), catalog.identities
    assert header(catalog, Col("t0", tier)) == "Tier"
    assert header(catalog, Col("t0", country)) == f"City{ARROW}country"     # the knowledgebase's column: its path
    assert header(catalog, Col("t0", 0)) == "Customer"     # the sheet's own key, though it holds the customers' Name
    assert answer_columns(Show("t0", (Col("t0", 0), Col("t0", tier)), (), listing=True), catalog) == ("Customer", "Tier")
    assert answer_columns(Show("t0", (Col("t0", tier),), (), listing=True), catalog) == ("Tier",)
    print("  PASS  an answer names a column a key reaches by its sheet's header")


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


def test_a_sheet_that_states_its_rates_is_read_at_its_own_rates() -> None:
    """A request that holds its own rate table, keyed by the money table's currency codes, is converted at the sheet's
    rates where it states the target (test_geo: 615 EUR at 1.08 and 100 GBP at 1.27 is 791.20 in USD, the uploaded
    sheet winning over the knowledgebase's rates); a target the sheet does not state is read at the knowledgebase's,
    and a code the sheet lists no rate for converts to nothing. The sheet's targets are offered beside the
    knowledgebase's."""
    sheet = (("USD", (("EUR", "1.08"), ("JPY", "0.0065"))),)
    money = MoneyLink("orders", 3, None, (2,), ("rate_to_gbp", "rate_to_usd"), AS_OF, sheet)
    _key, _plain, _connected_key, usd = _connected("USD", links=_links(money=(money,)))
    _, amounts = _column(usd.catalog, f"Amount{ARROW}USD")
    assert [None if value is None else round(value, 4) for value in amounts] == [108.0, 54.0, 13.0, 19.5, 10.8], amounts
    _key, _plain, _connected_key, gbp = _connected("GBP", links=_links(money=(money,)))
    _, pounds = _column(gbp.catalog, f"Amount{ARROW}GBP")
    assert [round(value, 4) for value in pounds] == [85.0, 42.5, 10.4, 15.6, 8.5], pounds     # the knowledgebase's
    partial = MoneyLink("orders", 3, None, (2,), ("rate_to_cad",), AS_OF, (("CAD", (("EUR", "1.5"),)),))
    _key, _plain, _connected_key, cad = _connected("CAD", links=_links(money=(partial,)))
    _, dollars = _column(cad.catalog, f"Amount{ARROW}CAD")
    assert dollars == [150.0, 75.0, None, None, 15.0], dollars     # JPY has no rate on the sheet
    assert conversion_targets(_links(money=(partial,))) == ["CAD"]
    print("  PASS  a sheet that states its rates is read at its own rates")


def test_the_calculation_registry_reads_a_program_by_the_requests_names() -> None:
    """The Decisions path's programs read store tables (_t0, c2_n), and the calculation registry reads the request's
    schema, so "total revenue in euros" on a sheet with no currency column asked "which currency are the amounts in"
    (the measure c1_n was no monetary column it knew) where production answered 350 with euros as the unit.
    engine/tables.py request_named gives the registry each store column by the request's table and header; a line's
    role stays as it is, and a column a link reaches keeps its path."""
    from engine.calculations import describe_computation
    from engine.program.language import Agg, Col, Equals
    from engine.program.lowering import to_query
    from engine.program.names import SUM
    from engine.program.store import request_name
    from engine.tables import request_named

    _key, plain, _connected_key, connected = _connected("USD")
    catalog = connected.catalog
    assert request_name(catalog, "_t0", "c2_n") == ("orders", "Amount")
    assert request_name(catalog, "_t0", "_role") == ("orders", "_role")
    usd = _names(catalog).index(f"Amount{ARROW}USD")
    assert request_name(catalog, "_t0", f"c{usd}_n") == ("orders", f"Amount{ARROW}USD")
    assert request_name(catalog, "orders", "Amount") == ("orders", "Amount"), "a request table's own name is kept"
    program = Agg(SUM, Col("t0", 2), "t0", (Equals(Col("t0", 3), "eur"),))
    named = request_named(describe_computation(to_query(program, catalog)), catalog)
    output = named.branches[0].outputs[0]
    assert [(column.table, column.name) for column in output.columns] == [("orders", "Amount")], output.columns
    assert {(fact.table, fact.column) for fact in named.branches[0].predicates} >= {("orders", "Currency")}, named.branches[0].predicates
    print("  PASS  the calculation registry reads a program by the request's names")


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


def test_the_spider_evaluator_connects_the_keys_serving_finds() -> None:
    """The Spider evaluator registers the request's own links as serving does (engine/prepared.py link_tables, whose
    table_links engine/knowledge_bridges.py request_links extends with the knowledgebase), so a key between two tables
    reaches the other table's columns; before, the evaluator registered no linker and read every database
    unconnected."""
    import inspect

    import spider.probe.full_eval as full_eval
    from engine.prepared import link_tables
    assert inspect.getsource(full_eval.predict).count("link_tables(database, enc, schema_fks)") == 1
    assert inspect.getsource(full_eval.ast_predict).count("link_tables(database, enc, schema_fks)") == 1
    with scratch_database() as database:
        link_tables(database, TableQuery(), None)
        tables = _tables()
        key, _prepared = prepared_workbook(tables)
        links = database.links(key, tables)
    assert links is not None, "the evaluator registers the request's links"
    assert links.keys == (KEY,), links.keys
    assert not (links.places or links.entities or links.money), "Spider has no knowledgebase"
    assert links.columns == {"orders": tuple(ORDERS["columns"]), "customers": tuple(CUSTOMERS["columns"])}


def test_the_retry_reads_what_the_links_reach() -> None:
    """The retry (engine/retry.py) reads the workbook the Decisions path reads (engine/tables.py TableQuery.workbook):
    each compiled table with the columns the request's links add to it, so a retried question reads a city's continent
    and a customer's tier. Before (6d239ad1) it read the sheet's own columns alone and a Retry on "total amount in
    Europe" could only say the sheet holds no continent. Contrast: the unconnected workbook has no such column."""
    from engine.retry import retry, workbook_tables
    from tests.test_sql_ast import _neon

    _key, plain, _connected_key, connected = _connected()
    tables = {table["name"]: table for table in workbook_tables(connected.catalog)}
    orders = tables["orders"]
    continent = orders["columns"].index(f"City{ARROW}country{ARROW}continent")
    assert [row[continent] for row in orders["rows"]] == ["Europe", "Europe", "Asia", "Asia", None], orders["rows"]
    assert [row[orders["columns"].index("Amount")] for row in orders["rows"]] == [100.0, 50.0, 2000.0, 3000.0, 10.0]
    assert f"Customer{ARROW}Tier" in orders["columns"], orders["columns"]
    assert all(ARROW not in name for name in workbook_tables(plain.catalog)[0]["columns"])
    europe = f'SELECT SUM("Amount") AS "total amount" FROM "orders" WHERE "City{ARROW}country{ARROW}continent" = \'Europe\''
    prompts, restore = _neon([{"answerable": True, "sql": europe, "reason": "Adds Amount over the European cities."}])
    try:
        with scratch_database():
            retried = retry("total amount in Europe", list(tables.values()), TableQuery.guard.__get__(TableQuery.__new__(TableQuery)))
    finally:
        restore()
    assert retried.answered and retried.rows == ((150,),), retried
    assert f'"City{ARROW}country{ARROW}continent" TEXT values: \'Europe\', \'Asia\'' in prompts[0], prompts[0]
    print("  PASS  the retry reads what the request's links reach")


TESTS = [
    test_the_retry_reads_what_the_links_reach,
    test_a_key_and_what_it_reaches_hold_the_other_tables_values,
    test_an_answer_names_a_column_a_key_reaches_by_its_sheets_header,
    test_a_place_column_reaches_its_country_continent_currency_and_population,
    test_a_key_reaches_the_other_tables_columns,
    test_a_money_column_reads_in_the_target_currency_on_the_as_of_day,
    test_a_sheet_that_states_its_rates_is_read_at_its_own_rates,
    test_the_calculation_registry_reads_a_program_by_the_requests_names,
    test_the_request_tables_hold_the_reached_columns,
    test_a_linked_value_is_read_as_one_across_the_key_and_its_column,
    test_a_place_column_few_cells_match_is_not_linked,
    test_a_key_whose_values_repeat_adds_nothing,
    test_a_link_on_another_column_at_that_place_is_not_placed,
    test_a_table_with_a_place_column_takes_no_entity_link,
    test_without_links_the_workbook_is_its_own,
    test_the_preparation_is_kept_by_what_the_links_reach,
    test_select_query_reads_the_connected_workbook_and_asks_the_currency_first,
    test_the_spider_evaluator_connects_the_keys_serving_finds,
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
