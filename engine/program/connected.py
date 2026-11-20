"""The connected catalog of one request: the columns a request table's own columns reach, as columns of that table.

The request's database holds what production connects for its tables (engine/prepared.py RequestLinks, which the world
layer builds, engine/knowledge_bridges.py request_links): the bridge of each routed place column and of each column of
named entities (its cells' matches in the knowledgebase, with the knowledgebase rows they reach), the keys between the
request's tables, and its money columns with the exchange rates of their days. ``links_on`` places them on the compiled
workbook, keeping a link only where its column is the request table's column at that place; ``reach`` reads each link's
columns on the request's database, as joins of the table's own columns to the bridge and the knowledgebase tables; and
``connect`` adds them to the table they start from, named by their path ("city → country", "city → country → continent",
"bank → country", "amount → USD", "customer → tier"). The decoder lists them as it lists any column, so the closed
questions offer them, and the request's tables (engine/program/store.py RequestTables) hold them as they hold any column.

Nothing here decides what connects to what: the routes, matches, keys, money columns and rates are production's.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass

from engine.entities import VALUE_ROUTE_MIN
from engine.prepared import RequestLinks
from engine.program.catalog import KIND_TABLE, Catalog, CatalogTable
from engine.program.semantics import cell_fold, fold
from engine.program.store import LINE, day_column, number_column, sql_table, text_column, write_table
from engine.sheet.model import Cell, Column
from engine.sheet.names import RECORD, V_NUMBER, V_TEXT
from engine.tables import qident, qlit

ARROW = " → "
# A place or entity link needs this many distinct matched cells: production types an entity column from three cells up
# (engine/knowledge_query.py prepare_table); two cells ("Yes", "No") can both be city names.
LINK_MIN_CELLS = 3
RATE_PREFIX = "rate_to_"     # a knowledgebase exchange-rate column, the currency it converts to after it


@dataclass(frozen=True)
class Reach:
    """One column a link reaches: its path of names, its kind, and its value over the link's join aliases ({r} is the
    link's number, u the table's own columns); ``direct``, a column of another request table, read from its cells."""
    path: tuple[str, ...]
    kind: str
    sql: str
    direct: int | None = None


# What each kind of routed column reaches, and the joins that reach it. The edges are engine/data/word_*.json's
# (city.country -> country.qid, state.country -> country.qid; filter attributes country and continent) and the columns
# are the ones production lets a question read (engine/knowledge_tables.py WORLD_COL_SYN: country, continent, currency,
# population). A city or a country can hold two rows for one key, so each join reads one row per key.
BRIDGE = ('LEFT JOIN (SELECT "value" AS cell, MIN({key}) AS k FROM main.{bridge} WHERE "column" = {column} GROUP BY "value") AS b{r} '
          'ON b{r}.cell = u.{cell}')
COUNTRY_OF = ('LEFT JOIN (SELECT "qid", MIN("name") AS name, MIN("continent") AS continent, MIN("currency") AS currency, MAX("population") AS population '
              'FROM knowledgebase."country" GROUP BY "qid") AS n{r} ON n{r}."qid" = {on}')
LABEL_OF = 'LEFT JOIN knowledgebase."_labels" AS {alias}{r} ON {alias}{r}."qid" = {on}'
WORLDS = {
    "city": {
        "key": '"world_key"',
        "joins": (BRIDGE,
                  'LEFT JOIN (SELECT "qid", MIN("country") AS country, MAX("population") AS population FROM knowledgebase."city" GROUP BY "qid") AS e{r} '
                  'ON e{r}."qid" = b{r}.k',
                  COUNTRY_OF.replace("{on}", "e{r}.country"),
                  LABEL_OF.replace("{alias}", "l").replace("{on}", "n{r}.continent")),
        "reaches": (Reach(("country",), V_TEXT, "n{r}.name"), Reach(("population",), V_NUMBER, "e{r}.population"),
                    Reach(("country", "continent"), V_TEXT, "l{r}.label"), Reach(("country", "currency"), V_TEXT, "n{r}.currency")),
    },
    "country": {
        "key": '"entity_qid"',
        "joins": (BRIDGE, COUNTRY_OF.replace("{on}", "b{r}.k"), LABEL_OF.replace("{alias}", "l").replace("{on}", "n{r}.continent")),
        "reaches": (Reach(("continent",), V_TEXT, "l{r}.label"), Reach(("currency",), V_TEXT, "n{r}.currency"), Reach(("population",), V_NUMBER, "n{r}.population")),
    },
    "state": {
        "key": '"world_key"',
        "joins": (BRIDGE,
                  'LEFT JOIN (SELECT "name", MIN("country") AS country, MIN("continent") AS continent FROM knowledgebase."u_s_state" GROUP BY "name") AS s{r} '
                  'ON s{r}."name" = b{r}.k',
                  LABEL_OF.replace("{alias}", "lc").replace("{on}", "s{r}.country"),
                  LABEL_OF.replace("{alias}", "l").replace("{on}", "s{r}.continent")),
        "reaches": (Reach(("country",), V_TEXT, "lc{r}.label"), Reach(("continent",), V_TEXT, "l{r}.label")),
    },
}


def entity_world(table: str) -> dict:
    """A column of named things (banks, hospitals): its bridge holds each cell's entity, the entity's table its
    country (engine/knowledge_query.py _serve_world_type: entity.country -> country.qid, then the continent)."""
    return {"key": '"world_key"',
            "joins": (BRIDGE,
                      'LEFT JOIN (SELECT "qid", MIN("country") AS country FROM knowledgebase.' + qident(table) + ' GROUP BY "qid") AS e{r} ON e{r}."qid" = b{r}.k',
                      COUNTRY_OF.replace("{on}", "e{r}.country"),
                      LABEL_OF.replace("{alias}", "l").replace("{on}", "n{r}.continent")),
            "reaches": (Reach(("country",), V_TEXT, "n{r}.name"), Reach(("country", "continent"), V_TEXT, "l{r}.label"))}


@dataclass(frozen=True)
class Link:
    """One routed or entity column of a compiled table and the bridge that holds its cells' matches."""
    table_id: str
    position: int
    column: str          # the column's name as the bridge holds it
    world_type: str      # city, country or state; an entity column's type label (bank, hospital)
    bridge: str          # the bridge table's name in main
    entity_table: str = ""   # an entity column's knowledgebase table (its type's label), else ""

    def world(self) -> dict:
        return WORLDS[self.world_type] if not self.entity_table else entity_world(self.entity_table)

    def joins(self, r: int) -> str:
        world = self.world()
        return " ".join(join.format(r=r, key=world["key"], bridge=qident(self.bridge), column=qlit(self.column), cell=qident(text_column(self.position)))
                        for join in world["joins"])

    @property
    def reaches(self) -> tuple[Reach, ...]:
        return self.world()["reaches"]

    def header(self, name: str) -> str:
        """The type production read an entity column as, when the column's own name does not say it."""
        return f"{name} ({self.entity_table})" if self.entity_table and fold(self.entity_table) != fold(name) else name

    def unit(self) -> str:
        return ""


@dataclass(frozen=True)
class Conversion:
    """A money column in the row's own currency, read in a target currency: the knowledgebase exchange rate of the
    row's currency code on the row's day (the as-of day for a table with no date column) times the amount, as
    production binds it (engine/knowledge_tables.py _world_rate_binding: on the fact table's currency and date). The
    column states the target as its unit, so a total over it is one figure."""
    table_id: str
    position: int                 # the money column
    currency: int                 # the currency-code column's position
    date: int | None              # the date column's position, or None
    rate_column: str              # rate_to_usd
    target: str                   # USD
    as_of: str                    # ISO day, read when the table has no date column

    def joins(self, r: int) -> str:
        day = f"u.{qident(day_column(self.date))}" if self.date is not None else qlit(self.as_of)
        return (f'LEFT JOIN (SELECT "currency_code" AS code, CAST("date" AS TEXT) AS day, MIN({qident(self.rate_column)}) AS rate FROM knowledgebase."exchange_rate" '
                f'GROUP BY "currency_code", "date") AS x{r} ON x{r}.code = UPPER(TRIM(u.{qident(text_column(self.currency))})) AND x{r}.day = {day}')

    @property
    def reaches(self) -> tuple[Reach, ...]:
        return (Reach((self.target,), V_NUMBER, f"u.{qident(number_column(self.position))} * x{{r}}.rate"),)

    def header(self, name: str) -> str:
        return name

    def unit(self) -> str:
        return self.target


@dataclass(frozen=True)
class Upload:
    """A column that is a key into another request table, as production finds it (engine/tables.py ingest: trusted
    joins between the request's tables). Every other column of that table is reached through the key, the columns that
    table itself reaches included ("customer → city → country")."""
    table_id: str
    position: int
    other: str                    # the other table's id
    key: int                      # the key column's position in it


def links_on(catalog: Catalog, links: RequestLinks, target: str | None = None) -> list:
    """The request's links placed on the compiled workbook: its routed place columns, its entity columns (on a table
    with no place column), its money columns read in ``target`` when the question asks for one, and its keys. A
    compiled table is its request table by tab, a column by its place, kept only where the compiled column is the
    request table's column at that place; a place or entity column is linked when its bridge holds at least
    LINK_MIN_CELLS of its distinct cells, a place column only when production routes on its cells (VALUE_ROUTE_MIN)."""
    by_tab = {table.tab: table for table in catalog.tables if table.kind == KIND_TABLE}

    def at(tab: str, position: int | None):
        """The compiled table holding the request table ``tab``'s column at ``position``, or None."""
        table = by_tab.get(tab)
        names = links.columns.get(tab, ())
        if table is None or position is None or position >= len(table.columns) or position >= len(names):
            return None
        return table if fold(table.columns[position].name) == fold(names[position]) else None

    out: list = []
    for place in links.places:
        table = at(place.tab, place.position)
        share = place.matched / place.cells if place.cells else 0.0
        if (table is not None and place.world_type in WORLDS and place.bridged and share >= VALUE_ROUTE_MIN
                and place.matched >= LINK_MIN_CELLS):
            out.append(Link(table.id, place.position, place.column, place.world_type, place.bridge))
    placed = {link.table_id for link in out}
    for entity in links.entities:
        table = at(entity.tab, entity.position)
        if (table is not None and not entity.placed and entity.bridged and entity.has_country and entity.matched >= LINK_MIN_CELLS
                and table.id not in placed):
            out.append(Link(table.id, entity.position, entity.column, entity.label, entity.bridge, entity.entity_table))
    if target is not None:
        rate = RATE_PREFIX + target.lower()
        for money in links.money:
            table = at(money.tab, money.currency)
            if table is None or rate not in money.rate_columns:
                continue
            date = money.date if at(money.tab, money.date) is not None and table.is_date_column(money.date) else None
            out.extend(Conversion(table.id, position, money.currency, date, rate, target, money.as_of)
                       for position in money.measures if at(money.tab, position) is not None)
    for key in links.keys:
        table, other = at(key.tab, key.position), at(key.other_tab, key.other_position)
        if table is not None and other is not None:
            out.append(Upload(table.id, key.position, other.id, key.other_position))
    return out


def conversion_targets(links: RequestLinks) -> list[str]:
    """The currencies the request's money columns can be read in: one per knowledgebase rate column."""
    return sorted({column[len(RATE_PREFIX):].upper() for money in links.money for column in money.rate_columns})


def linked_columns(links: list) -> frozenset[tuple[str, str]]:
    """Pairs of "<table>.<position>" column ids a key links, both ways: a key and the column it keys into hold the same
    values (engine/program/closed.py value_set_scores)."""
    return frozenset(pair for link in links if isinstance(link, Upload)
                     for pair in ((f"{link.table_id}.{link.position}", f"{link.other}.{link.key}"),
                                  (f"{link.other}.{link.key}", f"{link.table_id}.{link.position}")))


def _reach_table(table_id: str) -> str:
    return f"{sql_table(table_id)}_reach"


def reach(catalog: Catalog, links: list, connection) -> tuple:
    """What each place, entity and money link reaches on the request's database, line for line (None for a key, whose
    columns are read from the other table's cells): each linked table's own columns are held in a TEMP table for the
    joins, which read the bridges in main and the knowledgebase rows the request reached."""
    held = []
    try:
        out = []
        for r, link in enumerate(links):
            if isinstance(link, Upload):
                out.append(None)
                continue
            name = _reach_table(link.table_id)
            if name not in held:
                write_table(connection, name, catalog.table(link.table_id), temporary=True)
                held.append(name)
            selected = ", ".join(column.sql.format(r=r) for column in link.reaches)
            rows = connection.execute(f'SELECT {selected} FROM temp.{qident(name)} AS u {link.joins(r)} ORDER BY u.{qident(LINE)}').fetchall()
            if len(rows) != len(catalog.table(link.table_id).lines):
                raise RuntimeError(f"{link.table_id}: a link's join changes the number of lines")
            out.append(tuple(tuple(row) for row in rows))
        return tuple(out)
    finally:
        for name in held:
            connection.execute(f"DROP TABLE IF EXISTS temp.{qident(name)}")


def _cell(kind: str, value):
    if value is None or value == "":
        return None
    if kind == V_NUMBER:
        try:
            return Cell(-1, -1, float(value), V_NUMBER, float(value))
        except (TypeError, ValueError):
            return None
    return Cell(-1, -1, str(value), V_TEXT, str(value))


def connect(catalog: Catalog, links: list, reached: tuple) -> Catalog:
    """The catalog with every link's reached columns added to the table it starts from. A linked table is copied, so
    the workbook's own catalog is never changed; a key's columns follow the other table's own links, so what that
    table reaches is reached through the key too. A key whose values repeat in the other table adds nothing (the join
    would repeat lines), nor does a reached column no line fills."""
    copies = {link.table_id: CatalogTable(link.table_id, copy.deepcopy(catalog.table(link.table_id).table), catalog.table(link.table_id).kind,
                                          catalog.table(link.table_id).adds_twice)
              for link in links}
    plain = Catalog([copies.get(table.id, table) for table in catalog.tables], catalog.name)
    order = [r for r, link in enumerate(links) if not isinstance(link, Upload)]
    pending = [r for r, link in enumerate(links) if isinstance(link, Upload)]
    holds: dict[tuple[str, int], tuple[str, int]] = {}     # a column -> the column whose values it holds
    while pending:      # a table another table's key reads comes first
        ready = [r for r in pending if not any(links[q].table_id == links[r].other for q in pending if q != r)] or pending[:1]
        order.extend(ready)
        pending = [r for r in pending if r not in ready]
    for r in order:
        link = links[r]
        held = plain.table(link.table_id)
        table = held.table
        if isinstance(link, Upload):
            other = plain.table(link.other)
            keys: dict[str, int] = {}
            unique = True
            for j, line in enumerate(other.lines):
                key = cell_fold(line.cells.get(link.key)) if line.role == RECORD else None
                if key is not None:
                    unique = unique and key not in keys
                    keys[key] = j
            if not unique or not keys:
                continue
            holds[(link.table_id, link.position)] = (link.other, link.key)
            name = table.columns[link.position].name
            for q, column in enumerate(list(other.table.columns)):
                if q == link.key:
                    continue
                position = len(table.columns)
                filled, seen = 0, set()
                for line in held.lines:
                    key = cell_fold(line.cells.get(link.position)) if line.role == RECORD else None
                    cell = other.lines[keys[key]].cells.get(q) if key in keys else None
                    if cell is not None:
                        line.cells[position] = cell
                        filled += 1
                        seen.add(cell.value)
                if not filled:
                    continue
                holds[(link.table_id, position)] = (link.other, q)
                path = (name, *(column.path if column.path and column.col == -1 else (column.name,)))
                table.columns.append(Column(position, -1, "", ARROW.join(path), path=path, role=column.role, kind=column.kind, format_kind=column.format_kind,
                                            unit=column.unit, filled=filled, distinct=len(seen)))
            continue
        name = link.header(table.columns[link.position].name)
        for at, column in enumerate(link.reaches):
            position = len(table.columns)
            filled, seen = 0, set()
            for line, row in zip(held.lines, reached[r]):
                cell = _cell(column.kind, row[at]) if line.role == RECORD else None
                if cell is not None:
                    line.cells[position] = cell
                    filled += 1
                    seen.add(cell.value)
            if not filled:
                continue
            table.columns.append(Column(position, -1, "", ARROW.join((name, *column.path)), path=(name, *column.path),
                                        role="measure" if column.kind == V_NUMBER else "label", kind=column.kind, filled=filled, distinct=len(seen),
                                        unit=link.unit()))
    return Catalog([CatalogTable(table.id, table.table, table.kind, table.adds_twice) if table.id in copies else table for table in plain.tables],
                   catalog.name, {column: _root(holds, column) for column in holds})


def _root(holds: dict[tuple[str, int], tuple[str, int]], column: tuple[str, int]) -> tuple[str, int]:
    """The column at the end of ``column``'s chain of held values ("order_id → customer_id → customer_name" on
    order_items holds orders' "customer_id → customer_name", which holds customers' customer_name)."""
    seen = {column}
    while column in holds and holds[column] not in seen:
        column = holds[column]
        seen.add(column)
    return column


__all__ = ["ARROW", "Conversion", "LINK_MIN_CELLS", "Link", "Upload", "connect", "conversion_targets", "linked_columns", "links_on", "reach"]
