"""Lower the world owner's selected bindings, without reparsing rendered SQL.

Entity resolution precedes this module. A connected bridge pins the resolved QID;
SQLAlchemy secondary relationships expose the actual knowledgebase object directly
on the uploaded object (Order.city -> City -> Country).
"""

from __future__ import annotations

from dataclasses import replace

from engine.deterministic.lower import (
    UnsupportedDeterministicPlan,
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
):
    """Consume only grounded structural slots selected by KnowledgeTableQuery."""
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
                ColumnSpec(column, attrs[column], dtype, column in keys)
                for column, dtype in columns
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
    else:
        raise UnsupportedDeterministicPlan(
            "world projection requires typed projection bindings"
        )
    if isinstance(operand, BinaryValue):
        views.append(
            CalculatedView(
                f"{slug}_calculated",
                views[-1].name,
                (SelectedValue("calculated_value", operand),),
            )
        )
        operand = ViewValue("calculated_value")
    views.append(
        ReducedView(
            f"{slug}_total", views[-1].name, (AggregateValue(alias, function, operand),)
        )
    )
    return AnalysisPlan(slug, tuple(tables.values()), tuple(views))


def reference_schema(connection, required, keys=None):
    """Read declared physical types; never guess the type from the name of a fact."""
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
            (column, types[dtype])
            for column, dtype in rows
            if column in wanted and dtype in types
        ]
        if wanted - {column for column, _ in selected}:
            raise UnsupportedDeterministicPlan(
                f"missing or unsupported knowledgebase columns: {table}: {wanted - {column for column, _ in selected}}"
            )
        result[table] = selected
    return result
