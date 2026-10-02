"""Lower the world owner's selected bindings, without reparsing rendered SQL.

Entity resolution precedes this module. A connected bridge pins the resolved QID;
SQLAlchemy secondary relationships expose the actual knowledgebase object directly
on the uploaded object (Order.city -> City -> Country).
"""

from __future__ import annotations

from dataclasses import replace

from engine.deterministic.lower import (
    UnsupportedDeterministicPlan,
    _unique_identifiers,
    _unique_names,
    _unique_class_names,
    _value,
    lower_select_query,
)
from engine.deterministic.plan import (
    AggregateValue,
    AnalysisPlan,
    BinaryValue,
    CalculatedView,
    ColumnSpec,
    ColumnValue,
    EnrichedView,
    Enrichment,
    FilteredView,
    FunctionValue,
    JunctionValue,
    LiteralValue,
    PredicateValue,
    ReducedView,
    RelationshipSpec,
    SelectedValue,
    SortedView,
    SortValue,
    TableSpec,
    ViewValue,
    ProjectedView,
)
from engine.sql_ast import (
    Aggregate,
    ColumnRef,
    Join,
    SelectItem,
    SelectQuery,
    SQLType,
    Star,
)


def _eq(left, right):
    return PredicateValue(left, "=", right)


def _lower(value):
    return FunctionValue("LOWER", value)


def lower_world_query(
    *,
    slug,
    schema,
    uploaded,
    foreign_keys,
    joins,
    bridge_name,
    route_table,
    route_column,
    meaning_filter,
    own_filters,
    world_rate,
    as_of,
    aggregate,
    calculation,
    conversion,
    reference_columns,
    disambiguated=False,
    reference_keys=None,
    bridge_key="entity_qid",
    projection=None,
    outer=False,
    dimension=None,
    order=None,
):
    """Consume only grounded structural slots selected by KnowledgeTableQuery.

    ``dimension`` is the world attribute a projection names, as (reference table, column). The kept rows
    are grouped by it, so each shape ends like the own-data and compose trails, a grouped total:
    without an aggregate ('which continent is Tokyo in') each value with the number of rows holding it;
    with ``order`` ("DESC" or "ASC", 'which continent has the highest total amount') the aggregate per
    value and the top group; with a COUNT and no order ('how many continents') the number of distinct
    non-empty values. The reference value stays the stored QID in both programs; the trail shows its label.
    """
    ast_joins = []
    for table, fk in zip(uploaded[1:], foreign_keys, strict=True):
        local = fk.get("from_cols") or (fk["from_col"],)
        remote = fk.get("to_cols") or (fk["to_col"],)
        pairs = tuple(
            (ColumnRef(fk["from_table"], a), ColumnRef(fk["to_table"], b))
            for a, b in zip(local, remote, strict=True)
        )
        ast_joins.append(Join(table, *pairs[0], additional=pairs[1:]))
    base = lower_select_query(
        slug,
        SelectQuery(
            (SelectItem(Aggregate("COUNT", Star())),), uploaded[0], tuple(ast_joins)
        ),
        schema,
        foreign_keys,
        postgres_row_identity=True,
    )
    tables = {table.name: table for table in base.tables}
    names = list(uploaded) + list(reference_columns)
    if joins:
        names.append(bridge_name)
    attributes, classes = _unique_names(names), _unique_class_names(names)
    for name, columns in reference_columns.items():
        attrs = _unique_names(tuple(column[0] for column in columns))
        keys = set(
            (reference_keys or {}).get(name)
            or (("currency_code", "date") if name == "exchange_rate" else ("qid",))
        )
        tables[name] = TableSpec(
            name,
            classes[name],
            attributes[name],
            "knowledgebase",
            tuple(
                ColumnSpec(column, attrs[column], dtype, column in keys, binary_float=binary)
                for column, dtype, binary in columns
            ),
        )
    views = [replace(base.views[0], outer=outer)]
    if joins:
        # This class is a relationship association, not a flattened replacement for City.
        columns = ("column", "value", bridge_key) + (
            ("context",) if disambiguated else ()
        )
        attrs = _unique_names(columns)
        tables[bridge_name] = TableSpec(
            bridge_name,
            classes[bridge_name],
            attributes[bridge_name],
            "conversation",
            tuple(
                ColumnSpec(
                    c, attrs[c], SQLType.TEXT, c in {"column", "value", "context"}
                )
                for c in columns
            ),
        )
        for index, join in enumerate(joins):
            source_name, target_name = join["left_table"], join["right_table"]
            source = tables[source_name]
            local, remote = join["left_col"], join["right_col"]
            attribute = source.column(local).attribute
            if index == 0:
                predicates = (
                    _eq(
                        ColumnValue(source_name, local),
                        ColumnValue(bridge_name, "value"),
                    ),
                    _eq(ColumnValue(bridge_name, "column"), LiteralValue(route_column)),
                )
                if disambiguated:
                    predicates += (
                        _eq(
                            ColumnValue(source_name, disambiguated),
                            ColumnValue(bridge_name, "context"),
                        ),
                    )
                edge = RelationshipSpec(
                    attribute,
                    target_name,
                    (local,),
                    (remote,),
                    condition=JunctionValue("AND", predicates),
                    secondary=bridge_name,
                    secondary_condition=_eq(
                        ColumnValue(bridge_name, bridge_key),
                        ColumnValue(target_name, remote),
                    ),
                )
            else:
                edge = RelationshipSpec(
                    attribute,
                    target_name,
                    (local,),
                    (remote,),
                    condition=_eq(
                        _lower(ColumnValue(source_name, local)),
                        _lower(ColumnValue(target_name, remote)),
                    ),
                )
            tables[source_name] = replace(
                source, relationships=source.relationships + (edge,)
            )
            views.append(
                EnrichedView(
                    f"{slug}_enriched_{index + 1}",
                    views[-1].name,
                    (
                        Enrichment(
                            target_name, source_name, (attribute,), required=not outer
                        ),
                    ),
                )
            )
    predicates = []
    if meaning_filter:
        predicates.append(
            _eq(
                ColumnValue(meaning_filter["filter_table"], meaning_filter["attr"]),
                LiteralValue(meaning_filter["value"]),
            )
        )
    predicates.extend(
        _eq(_lower(ColumnValue(t, c)), _lower(LiteralValue(v)))
        for t, c, v in own_filters
    )
    if predicates:
        views.append(
            FilteredView(
                f"{slug}_filtered",
                views[-1].name,
                JunctionValue("AND", tuple(predicates)),
            )
        )
    if projection is not None:
        views.append(ProjectedView(f"{slug}_base", views[-1].name, tuple(projection)))
        return AnalysisPlan(slug, tuple(tables.values()), tuple(views))
    if world_rate:
        fact = tables[world_rate["fact"]]
        local = (world_rate["ccy_col"],)
        date_side = (
            ColumnValue(fact.name, world_rate["date_col"])
            if world_rate["date_col"]
            else LiteralValue(str(as_of))
        )
        condition = JunctionValue(
            "AND",
            (
                _eq(
                    _lower(ColumnValue(fact.name, local[0])),
                    _lower(ColumnValue("exchange_rate", "currency_code")),
                ),
                _eq(
                    FunctionValue("TEXT", ColumnValue("exchange_rate", "date")),
                    FunctionValue("TEXT", date_side),
                ),
            ),
        )
        edge = RelationshipSpec(
            "exchange_rate",
            "exchange_rate",
            local,
            ("currency_code",),
            condition=condition,
        )
        tables[fact.name] = replace(fact, relationships=fact.relationships + (edge,))
        views.append(
            EnrichedView(
                f"{slug}_rates",
                views[-1].name,
                (Enrichment("exchange_rate", fact.name, (edge.attribute,)),),
            )
        )
    if calculation is not None:
        expression = calculation.expression
        if not isinstance(expression, Aggregate):
            raise UnsupportedDeterministicPlan("world calculation must be an aggregate")
        function, operand, alias = (
            expression.function,
            _value(expression.operand),
            calculation.alias,
        )
    elif aggregate:
        function, table, column = aggregate
        operand = None if function == "COUNT" else ColumnValue(table, column)
        alias = function.lower()
        if world_rate or conversion:
            rate = (
                ("exchange_rate", world_rate["rate_col"]) if world_rate else conversion
            )
            operand = BinaryValue(operand, "*", ColumnValue(*rate))
            function = "SUM"
            if world_rate and world_rate.get("target"):
                # Named for its currency, as the registered conversion names a converted total
                # ("total_usd"): "which city has the highest total amount in US dollars?" headed its
                # total "sum" (Chrome gate, 2026-10-02).
                alias = f"total_{world_rate['target'].lower()}"
    elif dimension is not None:
        if order is not None:
            raise UnsupportedDeterministicPlan("a ranked world projection needs a SUM or AVG measure")
        function, operand, alias = "COUNT", None, "count"
    elif world_rate or conversion:
        raise UnsupportedDeterministicPlan("a currency conversion lowers only as an aggregate")
    else:
        # A listing ('amount in France', 'customers in France'): the kept rows are the answer, so the
        # last sheet (filtered, or the lookup when nothing is filtered) is the declared output
        # (docs/SHEETS_AS_REASONING.md rule 6). Repeating it as a projection sheet would be a no-op
        # sheet (rule 3). These listings used to fail here, and a leftover word such as 'amount' sent
        # them to the semantic search, which showed one opaque step.
        return AnalysisPlan(slug, tuple(tables.values()), tuple(views))
    if isinstance(operand, BinaryValue):
        views.append(
            CalculatedView(
                f"{slug}_calculated",
                views[-1].name,
                (SelectedValue("calculated_value", operand),),
            )
        )
        operand = ViewValue("calculated_value")
    if dimension is None:
        if order is not None:
            raise UnsupportedDeterministicPlan("a ranking needs the world attribute it ranks")
        views.append(
            ReducedView(
                f"{slug}_total", views[-1].name, (AggregateValue(alias, function, operand),)
            )
        )
        return AnalysisPlan(slug, tuple(tables.values()), tuple(views))
    if order not in (None, "DESC", "ASC"):
        raise UnsupportedDeterministicPlan(f"unsupported ranking order: {order!r}")
    if order is not None and function == "COUNT":
        raise UnsupportedDeterministicPlan("a ranked world projection needs a SUM or AVG measure")
    reference, attribute = dimension
    key, alias = _unique_identifiers((attribute, alias))
    group = SelectedValue(key, ColumnValue(reference, attribute))
    distinct_count = aggregate is not None and function == "COUNT" and order is None
    views.append(
        ReducedView(
            f"{slug}_groups" if distinct_count else f"{slug}_total",
            views[-1].name,
            (AggregateValue(alias, function, operand),),
            (group,),
        )
    )
    if distinct_count:
        # COUNT of the group key skips the rows with no value, as COUNT(DISTINCT ...) does.
        views.append(
            ReducedView(
                f"{slug}_total", views[-1].name, (AggregateValue(alias, "COUNT", ViewValue(key)),)
            )
        )
    elif order is not None:
        # The key breaks ties, so both programs keep the same group.
        views.append(
            SortedView(
                f"{slug}_top_results",
                views[-1].name,
                (SortValue(ViewValue(alias), order == "DESC"), SortValue(ViewValue(key))),
                1,
            )
        )
    return AnalysisPlan(slug, tuple(tables.values()), tuple(views))


def reference_schema(connection, required, keys=None):
    """Read declared physical types; never guess the type from the name of a fact.

    Each column is (name, SQLType, stored as a binary float): a double precision column is read as
    NUMERIC by both programs (ColumnSpec.binary_float)."""
    types = {
        "text": SQLType.TEXT,
        "character varying": SQLType.TEXT,
        "integer": SQLType.INTEGER,
        "bigint": SQLType.INTEGER,
        "numeric": SQLType.REAL,
        "double precision": SQLType.REAL,
        "boolean": SQLType.BOOLEAN,
        "date": SQLType.DATE,
    }
    result = {}
    for table, columns in required.items():
        rows = connection.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='knowledgebase' AND table_name=%s ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        wanted = set(columns) | set(
            (keys or {}).get(table)
            or (("date", "currency_code") if table == "exchange_rate" else ("qid",))
        )
        selected = [
            (column, types[dtype], dtype == "double precision")
            for column, dtype in rows
            if column in wanted and dtype in types
        ]
        if wanted - {column for column, _type, _binary in selected}:
            raise UnsupportedDeterministicPlan(
                "missing or unsupported knowledgebase columns: "
                f"{table}: {wanted - {column for column, _type, _binary in selected}}"
            )
        result[table] = selected
    return result
