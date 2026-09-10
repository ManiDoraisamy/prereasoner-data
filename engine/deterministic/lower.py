"""Lower the supported typed SQL AST subset into the shared dual-emitter plan."""

from __future__ import annotations

import keyword
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

from engine.deterministic.plan import (
    AggregateValue,
    AnalysisPlan,
    BinaryValue,
    CalculatedView,
    ColumnSpec,
    ColumnValue,
    CombinedView,
    FilteredView,
    JunctionValue,
    LiteralValue,
    PredicateValue,
    ProjectedView,
    ReducedView,
    RelationshipSpec,
    SelectedValue,
    TableSpec,
    Value,
    ViewValue,
)
from engine.numeric import coerce_numeric
from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
    BooleanExpr,
    ColumnRef,
    Comparison,
    Literal,
    SelectItem,
    SelectQuery,
    Star,
)
from engine.sql_ast import SQLType as ASTType


class UnsupportedDeterministicPlan(ValueError):
    """The SQL AST is valid, but is not yet in the deliberately small dual subset."""


def lower_select_query(
    slug: str,
    query: SelectQuery,
    schema: Sequence[Mapping[str, object]],
    foreign_keys: Sequence[Mapping[str, object]],
    *,
    postgres_row_identity: bool = False,
) -> AnalysisPlan:
    """Build a feed-forward plan without parsing or reverse-engineering rendered SQL."""
    if not isinstance(query.from_table, str) or query.from_alias is not None:
        raise UnsupportedDeterministicPlan(
            "derived tables and aliases are not supported"
        )
    scalar_aggregate = (
        bool(query.select)
        and not query.group_by
        and all(isinstance(item.expression, Aggregate) for item in query.select)
    )
    harmless_scalar_order = scalar_aggregate and (
        not query.order_by or query.limit in (None, 1)
    )
    if (
        query.having is not None
        or (query.order_by and not harmless_scalar_order)
        or (query.limit is not None and not harmless_scalar_order)
        or query.distinct
    ):
        raise UnsupportedDeterministicPlan(
            "HAVING, ORDER BY, LIMIT, and DISTINCT are not supported"
        )
    if any(join.alias is not None or join.kind != "INNER" for join in query.joins):
        raise UnsupportedDeterministicPlan("only unaliased inner joins are supported")

    table_order = (query.from_table,) + tuple(join.table for join in query.joins)
    if len(table_order) != len(set(table_order)):
        raise UnsupportedDeterministicPlan("self joins are not supported")
    columns_by_table: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for column in schema:
        columns_by_table[str(column["table"])].append(column)
    missing = [table for table in table_order if table not in columns_by_table]
    if missing:
        raise UnsupportedDeterministicPlan(f"schema is missing tables: {missing}")

    table_attributes = _unique_names(table_order)
    class_names = _unique_class_names(table_order)
    column_attributes = {
        table: _unique_names(
            tuple(str(column["name"]) for column in columns_by_table[table])
        )
        for table in table_order
    }
    edges = _relationship_edges(query, foreign_keys, set(table_order))
    scalar_edges = []
    for source, target, local, remote in edges:
        if _stable_orm_key(remote, columns_by_table[target]):
            scalar_edges.append((source, target, local, remote))
        elif _stable_orm_key(local, columns_by_table[source]):
            scalar_edges.append((target, source, remote, local))
        else:
            raise UnsupportedDeterministicPlan(
                "join has no proven scalar relationship target"
            )
    edges = tuple(scalar_edges)
    preferred_keys: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for source, target, local_columns, remote_columns in edges:
        del source, local_columns
        preferred_keys[target].append(remote_columns)
    primary_keys: dict[str, set[str]] = {}
    for table in table_order:
        id_columns = tuple(
            str(column["name"])
            for column in columns_by_table[table]
            if re.search(
                r"(^id$|_?id$|^index$|^pk$)",
                str(column["name"]),
                re.IGNORECASE,
            )
        )
        all_columns = tuple(str(column["name"]) for column in columns_by_table[table])
        candidates = [*preferred_keys[table]]
        candidates.extend((column,) for column in id_columns)
        if id_columns:
            candidates.append(id_columns)
        candidates.append(all_columns)
        selected = next(
            (
                candidate
                for candidate in dict.fromkeys(candidates)
                if _stable_orm_key(candidate, columns_by_table[table])
            ),
            None,
        )
        if selected is None and postgres_row_identity:
            # PostgreSQL's tuple identity is unique within the repeatable-read
            # snapshot. It preserves duplicate input rows without changing CSVs.
            selected = ("ctid",)
        if selected is None:
            raise UnsupportedDeterministicPlan(
                f"table {table!r} has no stable ORM identity for this dataset"
            )
        primary_keys[table] = set(selected)

    relationships_by_table: dict[str, list[RelationshipSpec]] = defaultdict(list)
    used_relationship_attributes: dict[str, set[str]] = defaultdict(set)
    for source, target, local_columns, remote_columns in edges:
        proposed = (
            column_attributes[source][local_columns[0]]
            if len(local_columns) == 1
            else table_attributes[target]
        )
        attribute = _next_unique(proposed, used_relationship_attributes[source])
        relationships_by_table[source].append(
            RelationshipSpec(attribute, target, local_columns, remote_columns)
        )

    table_specs = tuple(
        TableSpec(
            name=table,
            class_name=class_names[table],
            attribute=table_attributes[table],
            schema="conversation",
            columns=tuple(
                ColumnSpec(
                    name=str(column["name"]),
                    attribute=column_attributes[table][str(column["name"])],
                    type=_column_type(column),
                    primary_key=str(column["name"]) in primary_keys[table],
                    nullable=_column_nullable(column),
                )
                for column in columns_by_table[table]
            )
            + (
                (
                    ColumnSpec(
                        "ctid",
                        _next_unique(
                            "_orm_row_identity", set(column_attributes[table].values())
                        ),
                        ASTType.TEXT,
                        True,
                        False,
                    ),
                )
                if "ctid" in primary_keys[table]
                else ()
            ),
            relationships=tuple(relationships_by_table[table]),
        )
        for table in table_order
    )

    views = [CombinedView(f"{slug}_combined", table_order)]
    if query.where is not None:
        views.append(
            FilteredView(
                f"{slug}_filtered",
                views[-1].name,
                _predicate(query.where),
            )
        )

    aggregates = [
        item for item in query.select if isinstance(item.expression, Aggregate)
    ]
    if aggregates:
        if any(
            not isinstance(item.expression, (Aggregate, ColumnRef))
            for item in query.select
        ):
            raise UnsupportedDeterministicPlan(
                "grouped projections may contain only group columns and aggregates"
            )
        calculations = []
        aggregate_values = []
        output_names = _select_names(query.select)
        for index, (item, output_name) in enumerate(
            zip(query.select, output_names, strict=True)
        ):
            expression = item.expression
            if not isinstance(expression, Aggregate):
                continue
            if expression.distinct:
                raise UnsupportedDeterministicPlan(
                    "DISTINCT aggregates are not supported"
                )
            if isinstance(expression.operand, Star):
                operand = None
            elif isinstance(expression.operand, BinaryExpr):
                calculated_name = f"aggregate_operand_{index + 1}"
                calculations.append(
                    SelectedValue(calculated_name, _value(expression.operand))
                )
                operand = ViewValue(calculated_name)
            else:
                operand = _value(expression.operand)
            aggregate_values.append(
                AggregateValue(output_name, expression.function, operand)
            )
        if calculations:
            views.append(
                CalculatedView(
                    f"{slug}_calculated", views[-1].name, tuple(calculations)
                )
            )
        group_items = [
            item for item in query.select if isinstance(item.expression, ColumnRef)
        ]
        if (
            {item.expression for item in group_items} != set(query.group_by)
            or list(query.select[: len(group_items)]) != group_items
        ):
            raise UnsupportedDeterministicPlan(
                "projected group columns must match GROUP BY and precede aggregates"
            )
        grouped = [
            SelectedValue(name, _value(item.expression))
            for item, name in zip(query.select, output_names, strict=True)
            if isinstance(item.expression, ColumnRef)
        ]
        views.append(
            ReducedView(
                f"{slug}_total",
                views[-1].name,
                tuple(aggregate_values),
                tuple(grouped),
            )
        )
    else:
        if query.group_by:
            raise UnsupportedDeterministicPlan(
                "GROUP BY without an aggregate is not supported"
            )
        if any(isinstance(item.expression, Star) for item in query.select):
            raise UnsupportedDeterministicPlan("SELECT * projection is not supported")
        values = tuple(
            SelectedValue(name, _value(item.expression))
            for item, name in zip(
                query.select, _select_names(query.select), strict=True
            )
        )
        views.append(ProjectedView(f"{slug}_result", views[-1].name, values))
    try:
        return AnalysisPlan(slug, table_specs, tuple(views))
    except (ValueError, TypeError) as exc:
        raise UnsupportedDeterministicPlan(str(exc)) from exc


def _relationship_edges(query, foreign_keys, included):
    declared = []
    for foreign_key in foreign_keys:
        local = foreign_key.get("from_cols") or (foreign_key.get("from_col"),)
        remote = foreign_key.get("to_cols") or (foreign_key.get("to_col"),)
        if None not in local and None not in remote:
            edge = (
                str(foreign_key["from_table"]),
                str(foreign_key["to_table"]),
                tuple(map(str, local)),
                tuple(map(str, remote)),
            )
            if edge[0] in included and edge[1] in included:
                declared.append(edge)

    edges = []
    joined = {query.from_table}
    for join in query.joins:
        pairs = join.predicates
        new_table = join.table
        if all(
            left.table == new_table and right.table in joined for left, right in pairs
        ):
            owners = {right.table for _, right in pairs}
            if len(owners) != 1:
                raise UnsupportedDeterministicPlan(
                    "a composite join must have one existing owner"
                )
            fallback = (
                new_table,
                owners.pop(),
                tuple(left.name for left, _ in pairs),
                tuple(right.name for _, right in pairs),
            )
        elif all(
            right.table == new_table and left.table in joined for left, right in pairs
        ):
            owners = {left.table for left, _ in pairs}
            if len(owners) != 1:
                raise UnsupportedDeterministicPlan(
                    "a composite join must have one existing owner"
                )
            fallback = (
                new_table,
                owners.pop(),
                tuple(right.name for _, right in pairs),
                tuple(left.name for left, _ in pairs),
            )
        else:
            raise UnsupportedDeterministicPlan(
                "join does not extend the existing table graph"
            )
        join_pairs = {
            (left.table, left.name, right.table, right.name) for left, right in pairs
        }
        matching = [
            edge
            for edge in declared
            if join_pairs
            == {
                (edge[0], local, edge[1], remote)
                for local, remote in zip(edge[2], edge[3], strict=True)
            }
            or join_pairs
            == {
                (edge[1], remote, edge[0], local)
                for local, remote in zip(edge[2], edge[3], strict=True)
            }
        ]
        edge = matching[0] if matching else fallback
        if edge not in edges:
            edges.append(edge)
        joined.add(new_table)
    return tuple(edges)


def _predicate(value) -> PredicateValue:
    if isinstance(value, BooleanExpr):
        return JunctionValue(
            value.operator, tuple(_predicate(term) for term in value.terms)
        )
    if not isinstance(value, Comparison):
        raise UnsupportedDeterministicPlan(
            "only one non-aggregate comparison is supported"
        )
    if value.operator not in {"=", "!=", "<>", ">", "<", ">=", "<=", "IS", "IS NOT"}:
        raise UnsupportedDeterministicPlan(
            f"comparison {value.operator!r} is not supported"
        )
    return PredicateValue(_value(value.left), value.operator, _value(value.right))


def _value(value) -> Value:
    if isinstance(value, ColumnRef):
        return ColumnValue(value.table, value.name)
    if isinstance(value, Literal):
        literal = value.value
        if value.type is ASTType.DATE and isinstance(literal, str):
            try:
                literal = date.fromisoformat(literal)
            except ValueError as exc:
                raise UnsupportedDeterministicPlan(
                    f"invalid ISO date literal: {literal!r}"
                ) from exc
        elif isinstance(literal, float):
            literal = Decimal(str(literal))
        return LiteralValue(literal)
    if isinstance(value, BinaryExpr):
        return BinaryValue(_value(value.left), value.operator, _value(value.right))
    raise UnsupportedDeterministicPlan(
        f"expression {type(value).__name__} is not supported"
    )


def _select_names(items: Sequence[SelectItem]) -> tuple[str, ...]:
    proposed = []
    for item in items:
        if item.alias:
            proposed.append(item.alias)
        elif isinstance(item.expression, ColumnRef):
            proposed.append(item.expression.name)
        elif isinstance(item.expression, Aggregate):
            proposed.append(item.expression.function.lower())
        else:
            proposed.append("value")
    return _unique_identifiers(tuple(proposed))


def _column_type(column: Mapping[str, object]) -> ASTType:
    if column.get("is_date"):
        return ASTType.DATE
    return {
        "INTEGER": ASTType.INTEGER,
        "REAL": ASTType.REAL,
        "BOOLEAN": ASTType.BOOLEAN,
        "TEXT": ASTType.TEXT,
    }.get(str(column.get("affinity", "TEXT")).upper(), ASTType.UNKNOWN)


def _column_nullable(column: Mapping[str, object]) -> bool:
    values = column.get("values")
    return not isinstance(values, Sequence) or any(
        value is None or (isinstance(value, str) and not value.strip())
        for value in values
    )


def _stable_orm_key(
    candidate: tuple[str, ...], columns: Sequence[Mapping[str, object]]
) -> bool:
    if not candidate:
        return False
    by_name = {str(column["name"]): column for column in columns}
    raw_values = [by_name[name].get("values") for name in candidate]
    if any(
        not isinstance(values, Sequence) or isinstance(values, (str, bytes))
        for values in raw_values
    ):
        return False
    lengths = {len(values) for values in raw_values}
    if len(lengths) != 1:
        return False
    seen = set()
    for row in zip(*raw_values, strict=True):
        # Identity must be unique after the same numeric coercion used by upload.
        row = tuple(
            coerce_numeric(value, str(by_name[name].get("affinity", "TEXT")))
            if by_name[name].get("affinity") in {"INTEGER", "REAL"}
            else value
            for name, value in zip(candidate, row, strict=True)
        )
        if any(
            value is None or (isinstance(value, str) and not value.strip())
            for value in row
        ):
            return False
        if row in seen:
            return False
        seen.add(row)
    return True


def _unique_names(values: Sequence[str]) -> dict[str, str]:
    used = set()
    result = {}
    for value in values:
        result[value] = _next_unique(_identifier(value), used)
    return result


def _unique_identifiers(values: Sequence[str]) -> tuple[str, ...]:
    used = set()
    return tuple(_next_unique(_identifier(value), used) for value in values)


def _unique_class_names(values: Sequence[str]) -> dict[str, str]:
    used = set()
    result = {}
    for value in values:
        singular = _singular(value)
        proposed = "".join(
            piece.capitalize() for piece in _identifier(singular).split("_")
        )
        result[value] = _next_unique(proposed or "Generated", used)
    return result


def _next_unique(proposed: str, used: set[str]) -> str:
    candidate = proposed
    suffix = 2
    while candidate in used:
        candidate = f"{proposed}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _identifier(value: str) -> str:
    name = re.sub(r"\W+", "_", str(value), flags=re.UNICODE).strip("_").lower()
    if not name:
        name = "value"
    if name[0].isdigit():
        name = "value_" + name
    if keyword.iskeyword(name) or name in {"base", "metadata", "registry", "session"}:
        name += "_value"
    return name


def _singular(value: str) -> str:
    if value.endswith("ies") and len(value) > 3:
        return value[:-3] + "y"
    if value.endswith("ses") and len(value) > 3:
        return value[:-2]
    if value.endswith("s") and not value.endswith("ss"):
        return value[:-1]
    return value
