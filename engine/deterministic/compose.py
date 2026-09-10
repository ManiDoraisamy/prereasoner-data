"""Lower ComposeEngine's selected primitive bindings to the shared view plan."""

from __future__ import annotations

from engine.deterministic.lower import UnsupportedDeterministicPlan, _unique_names
from engine.deterministic.plan import (
    AggregateValue,
    AnalysisPlan,
    BinaryValue,
    ColumnValue,
    FilteredView,
    JunctionValue,
    LiteralValue,
    PredicateValue,
    ProjectedView,
    ReducedView,
    SelectedValue,
    SortedView,
    SortValue,
    ViewValue,
    WindowView,
)
from engine.deterministic.world import lower_world_query, reference_schema


def lower_composition(slug, tables, schema, bindings, world, connection):
    graph = (world or {}).get("graph")
    if bindings["world_join"] and graph is None:
        raise UnsupportedDeterministicPlan(
            "composition requires resolved ORM reference bindings"
        )
    by_name = {table["name"]: table for table in tables}
    selected = bindings["uploaded_join"]
    fact = selected[0] if selected else tables[0]["name"]
    uploaded = [fact]
    foreign_keys = []
    columns = {column: ColumnValue(fact, column) for column in by_name[fact]["columns"]}
    if selected:
        for dimension, local, remote, keep in selected[1]:
            uploaded.append(dimension)
            foreign_keys.append(
                {
                    "from_table": fact,
                    "from_col": local,
                    "to_table": dimension,
                    "to_col": remote,
                }
            )
            columns.update((column, ColumnValue(dimension, column)) for column in keep)
    own_filters = []
    if bindings["value_filter"]:
        column, value = bindings["value_filter"]
        source = columns[column]
        own_filters.append((source.table, source.column, value))
    meaning_filter = None
    required = {}
    edges, keys = [], {}
    if bindings["world_join"]:
        _, _, keep, filter_column, filter_value = bindings["world_join"]
        edges, keys = graph["edges"], graph["keys"]
        for column in keep:
            if column not in graph["columns"]:
                raise UnsupportedDeterministicPlan(
                    f"world attribute has no source relationship: {column}"
                )
            columns[column] = ColumnValue(*graph["columns"][column])
        for edge in edges:
            required.setdefault(edge["right_table"], set()).add(edge["right_col"])
            if edge["left_table"] not in by_name:
                required.setdefault(edge["left_table"], set()).add(edge["left_col"])
        for column in columns.values():
            if column.table not in by_name:
                required.setdefault(column.table, set()).add(column.column)
        if filter_column and filter_value is not None:
            source = columns[filter_column]
            meaning_filter = {
                "filter_table": source.table,
                "attr": source.column,
                "value": filter_value,
            }
    names = _unique_names(tuple(columns))
    base = lower_world_query(
        slug=slug,
        schema=schema,
        uploaded=uploaded,
        foreign_keys=foreign_keys,
        joins=edges,
        bridge_name=graph["bridge"] if edges else None,
        route_table=edges[0]["left_table"] if edges else fact,
        route_column=edges[0]["left_col"] if edges else None,
        meaning_filter=meaning_filter,
        own_filters=own_filters,
        world_rate=None,
        as_of=None,
        aggregate=None,
        calculation=None,
        conversion=None,
        reference_columns=reference_schema(connection, required, keys),
        reference_keys=keys,
        bridge_key="world_key",
        outer=True,
        projection=tuple(
            SelectedValue(names[column], source) for column, source in columns.items()
        ),
    )
    views = list(base.views)
    columns = {column: ViewValue(name) for column, name in names.items()}
    for index, step in enumerate(bindings["steps"]):
        operation = step["op"]
        name = f"{slug}_{operation}_{index + 1}"
        source = views[-1].name
        if operation in {"filter", "time_filter", "having"}:
            predicate = JunctionValue(
                "AND",
                tuple(
                    PredicateValue(columns[column], operator, LiteralValue(value))
                    for column, operator, value in step["conds"]
                ),
            )
            views.append(FilteredView(name, source, predicate))
        elif operation == "group_agg":
            names = _unique_names(
                tuple(step["by"]) + tuple(out for _, _, out in step["aggs"])
            )
            groups = tuple(
                SelectedValue(names[column], columns[column]) for column in step["by"]
            )
            aggregates = tuple(
                AggregateValue(
                    names[out],
                    function,
                    None if column in (None, "*") else columns[column],
                )
                for function, column, out in step["aggs"]
            )
            views.append(ReducedView(name, source, aggregates, groups))
            columns = {column: ViewValue(alias) for column, alias in names.items()}
        elif operation == "divide":
            keep = list(
                dict.fromkeys((step.get("keep") or []) + [step["num"], step["den"]])
            )
            names = _unique_names(tuple(keep) + (step["outcol"],))
            values = tuple(
                SelectedValue(names[column], columns[column]) for column in keep
            )
            values += (
                SelectedValue(
                    names[step["outcol"]],
                    BinaryValue(columns[step["num"]], "/", columns[step["den"]]),
                ),
            )
            views.append(ProjectedView(name, source, values))
            columns = {column: ViewValue(alias) for column, alias in names.items()}
        elif operation in {"share", "running", "yoy"}:
            out = _unique_names(tuple(columns) + (step["outcol"],))[step["outcol"]]
            partition = (columns[step["key"]],) if step.get("key") else ()
            views.append(
                WindowView(
                    name,
                    source,
                    operation,
                    out,
                    columns[step["measure"]],
                    partition,
                    columns[step["time"]] if step.get("time") else None,
                )
            )
            columns[step["outcol"]] = ViewValue(out)
        elif operation in {"topn", "sort"}:
            order = [SortValue(columns[step["order"]], step["desc"])]
            # A tied top-N must choose the same rows in both programs.
            order.extend(
                SortValue(columns[column])
                for column in sorted(columns)
                if column != step["order"]
            )
            views.append(SortedView(name, source, tuple(order), step["n"]))
            if step.get("select"):
                names = _unique_names(tuple(step["select"]))
                views.append(
                    ProjectedView(
                        name + "_select",
                        name,
                        tuple(
                            SelectedValue(names[column], columns[column])
                            for column in step["select"]
                        ),
                    )
                )
                columns = {column: ViewValue(alias) for column, alias in names.items()}
        else:
            raise UnsupportedDeterministicPlan(
                f"unsupported composition operator: {operation}"
            )
    if not isinstance(views[-1], (ReducedView, ProjectedView)):
        names = _unique_names(tuple(columns))
        views.append(
            ProjectedView(
                f"{slug}_result",
                views[-1].name,
                tuple(
                    SelectedValue(names[column], value)
                    for column, value in columns.items()
                ),
            )
        )
    return AnalysisPlan(slug, base.tables, tuple(views))
