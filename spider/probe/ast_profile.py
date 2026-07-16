"""Comparable structural profiles for Spider gold SQL and planner ASTs.

The profiler is evaluation-only.  It turns Spider's parsed SQL dictionaries and
the engine's typed AST into the same schema-independent sketch plus role-aware
schema references.  Failure analysis can therefore distinguish grammar recall
from schema linking without parsing rendered SQL strings.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from engine.sql_profile import SCHEMA_ROLES, SQLProfile, canonical_name, profile_query

try:
    from .hardness import AGG_OPS, UNIT_OPS, WHERE_OPS
except ImportError:  # direct script execution
    from hardness import AGG_OPS, UNIT_OPS, WHERE_OPS


class _ProfileBuilder:
    def __init__(self) -> None:
        self.sketch: Counter[str] = Counter()
        self.tables: set[str] = set()
        self.roles: defaultdict[str, set[str]] = defaultdict(set)

    def feature(self, name: str, count: int = 1) -> None:
        if count:
            self.sketch[name] += int(count)

    def table(self, name: Any) -> str:
        normalized = canonical_name(name)
        if normalized:
            self.tables.add(normalized)
        return normalized

    def column(self, table: Any, column: Any, *roles: str) -> None:
        table_name = self.table(table)
        column_name = canonical_name(column)
        if not table_name or not column_name or column_name == "star":
            return
        qualified = f"{table_name}.{column_name}"
        for role in roles:
            self.roles[role].add(qualified)

    def finish(self) -> SQLProfile:
        return SQLProfile.build(self.sketch, self.tables, self.roles)


class _SpiderProfileBuilder(_ProfileBuilder):
    def __init__(self, metadata: Mapping[str, Any]) -> None:
        super().__init__()
        self.table_names = tuple(metadata["table_names_original"])
        self.column_names = tuple(metadata["column_names_original"])

    def visit(self, sql: Mapping[str, Any]) -> None:
        self.feature("blocks")
        select = sql.get("select", (False, ()))
        select_items = select[1] if len(select) > 1 else ()
        self.feature("select_items", len(select_items))
        if select and bool(select[0]):
            self.feature("distinct")
        for aggregate_id, value_unit in select_items:
            aggregate = self._aggregate_name(aggregate_id)
            if aggregate:
                self.feature(f"aggregate.{aggregate}")
            self._value_unit(value_unit, "projection", aggregate=bool(aggregate))

        from_clause = sql.get("from", {})
        table_units = from_clause.get("table_units", ())
        base_tables = []
        for kind, value in table_units:
            if kind == "table_unit" and isinstance(value, int):
                base_tables.append(value)
                if 0 <= value < len(self.table_names):
                    self.table(self.table_names[value])
            elif isinstance(value, Mapping):
                self.feature("subquery.from")
                self.visit(value)
        self.feature("from_tables", len(base_tables))
        self.feature("joins", max(0, len(table_units) - 1))
        if len(base_tables) != len(set(base_tables)):
            self.feature("self_join")
        self._conditions(from_clause.get("conds", ()), "join")

        self._conditions(sql.get("where", ()), "filter")
        group_by = sql.get("groupBy", ())
        self.feature("group_items", len(group_by))
        for column_unit in group_by:
            self._column_unit(column_unit, "group")
        self._conditions(sql.get("having", ()), "having")

        order_by = sql.get("orderBy", ())
        if order_by:
            direction, value_units = order_by
            self.feature(f"order.{str(direction).lower()}", len(value_units))
            for value_unit in value_units:
                self._value_unit(value_unit, "order")
        if sql.get("limit") is not None:
            self.feature("limit")

        for operator in ("intersect", "union", "except"):
            branch = sql.get(operator)
            if isinstance(branch, Mapping):
                self.feature(f"set.{operator}")
                self.visit(branch)

    def _aggregate_name(self, aggregate_id: Any) -> str | None:
        if not isinstance(aggregate_id, int) or not 0 <= aggregate_id < len(AGG_OPS):
            return None
        name = AGG_OPS[aggregate_id]
        return None if name == "none" else str(name).upper()

    def _column_unit(self, value: Any, role: str, aggregate: bool = False) -> None:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            return
        aggregate_name = self._aggregate_name(value[0])
        if aggregate_name:
            self.feature(f"aggregate.{aggregate_name}")
            aggregate = True
        column_id = value[1]
        if not isinstance(column_id, int) or not 0 <= column_id < len(self.column_names):
            return
        table_id, column_name = self.column_names[column_id]
        if not isinstance(table_id, int) or not 0 <= table_id < len(self.table_names):
            return
        roles = [role]
        if aggregate:
            roles.append("aggregate")
        self.column(self.table_names[table_id], column_name, *roles)
        if len(value) > 2 and bool(value[2]):
            self.feature("distinct")

    def _value_unit(self, value: Any, role: str, aggregate: bool = False) -> None:
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            return
        unit_id = value[0]
        if isinstance(unit_id, int) and 0 <= unit_id < len(UNIT_OPS):
            operation = UNIT_OPS[unit_id]
            if operation != "none":
                self.feature(f"arithmetic.{operation}")
        self._column_unit(value[1], role, aggregate)
        if len(value) > 2 and value[2] is not None:
            self._column_unit(value[2], role, aggregate)

    def _conditions(self, values: Sequence[Any], role: str) -> None:
        conditions = values[::2]
        self.feature(f"{role}_predicates", len(conditions))
        for connector in values[1::2]:
            self.feature(f"boolean.{str(connector).lower()}")
        for condition in conditions:
            if not isinstance(condition, (list, tuple)) or len(condition) < 5:
                continue
            negated, operator_id, value_unit, first, second = condition[:5]
            if negated:
                self.feature("predicate.not")
            if isinstance(operator_id, int) and 0 <= operator_id < len(WHERE_OPS):
                self.feature(f"predicate.{WHERE_OPS[operator_id]}")
            self._value_unit(value_unit, role)
            self._condition_value(first, role)
            self._condition_value(second, role)

    def _condition_value(self, value: Any, role: str) -> None:
        if isinstance(value, Mapping):
            self.feature("subquery.predicate")
            self.visit(value)
            return
        if (
            isinstance(value, (list, tuple))
            and len(value) >= 3
            and isinstance(value[0], int)
            and isinstance(value[1], int)
            and isinstance(value[2], bool)
        ):
            self._column_unit(value, role)


def profile_spider_sql(sql: Mapping[str, Any], metadata: Mapping[str, Any]) -> SQLProfile:
    """Profile a Spider parsed SQL dictionary recursively."""
    builder = _SpiderProfileBuilder(metadata)
    builder.visit(sql)
    return builder.finish()


@dataclass(frozen=True)
class CandidateAssessment:
    rank: int
    sql: str
    profile: SQLProfile
    executable: bool = True
    lenient: bool = False
    strict: bool = False


def sketch_distance(left: SQLProfile, right: SQLProfile) -> int:
    """L1 distance over counted structural features."""
    a, b = Counter(left.sketch_map), Counter(right.sketch_map)
    return sum(abs(a[name] - b[name]) for name in a.keys() | b.keys())


def sketch_delta(gold: SQLProfile, candidate: SQLProfile) -> tuple[dict[str, int], dict[str, int]]:
    """Return counted features missing from and added by a candidate sketch."""
    expected, actual = gold.sketch_map, candidate.sketch_map
    names = expected.keys() | actual.keys()
    missing = {
        name: expected.get(name, 0) - actual.get(name, 0)
        for name in names
        if expected.get(name, 0) > actual.get(name, 0)
    }
    extra = {
        name: actual.get(name, 0) - expected.get(name, 0)
        for name in names
        if actual.get(name, 0) > expected.get(name, 0)
    }
    return dict(sorted(missing.items())), dict(sorted(extra.items()))


def _role_missing(gold: SQLProfile, candidates: Sequence[CandidateAssessment]) -> dict[str, list[str]]:
    available: defaultdict[str, set[str]] = defaultdict(set)
    for candidate in candidates:
        for role, columns in candidate.profile.roles:
            available[role].update(columns)
    return {
        role: sorted(set(columns) - available[role])
        for role, columns in gold.roles
        if set(columns) - available[role]
    }


def _candidate_schema_match(gold: SQLProfile, candidate: SQLProfile) -> bool:
    if not set(gold.tables) <= set(candidate.tables):
        return False
    candidate_roles = candidate.role_map
    return all(
        set(columns) <= set(candidate_roles.get(role, ()))
        for role, columns in gold.roles
    )


def diagnose_pool(
    gold: SQLProfile, candidates: Sequence[CandidateAssessment]
) -> dict[str, Any]:
    """Label a pool and expose independent recall/linking diagnostics."""
    candidates = tuple(candidates)
    strict_ranks = sorted(candidate.rank for candidate in candidates if candidate.strict)
    lenient_ranks = sorted(candidate.rank for candidate in candidates if candidate.lenient)
    executable = [candidate for candidate in candidates if candidate.executable]
    if 0 in strict_ranks:
        status = "top1_strict"
    elif strict_ranks:
        status = "strict_in_pool"
    elif lenient_ranks:
        status = "lenient_only"
    elif not candidates:
        status = "no_candidate"
    elif not executable:
        status = "execution_failure"
    else:
        status = "no_match"

    sketch_matches = [
        candidate for candidate in candidates if candidate.profile.sketch == gold.sketch
    ]
    schema_matches = [
        candidate
        for candidate in candidates
        if _candidate_schema_match(gold, candidate.profile)
    ]
    combined_matches = [
        candidate for candidate in sketch_matches
        if _candidate_schema_match(gold, candidate.profile)
    ]
    available_tables = {table for candidate in candidates for table in candidate.profile.tables}
    missing_tables = sorted(set(gold.tables) - available_tables)
    missing_roles = _role_missing(gold, candidates)

    feature_max: Counter[str] = Counter()
    for candidate in candidates:
        for name, value in candidate.profile.sketch:
            feature_max[name] = max(feature_max[name], value)
    missing_sketch = {
        name: value - feature_max[name]
        for name, value in gold.sketch
        if value > feature_max[name]
    }

    bottleneck = None
    if status in {"no_candidate", "execution_failure"}:
        bottleneck = status
    elif status == "no_match":
        if not sketch_matches:
            bottleneck = "missing_sketch"
        elif missing_tables:
            bottleneck = "missing_table_link"
        elif missing_roles:
            bottleneck = "missing_column_link"
        elif not combined_matches:
            bottleneck = "missing_composition"
        else:
            bottleneck = "value_or_semantic_mismatch"

    nearest = None
    if candidates:
        def key(candidate: CandidateAssessment) -> tuple[int, int, int, str]:
            schema_gap = len(set(gold.tables) - set(candidate.profile.tables))
            roles = candidate.profile.role_map
            schema_gap += sum(
                len(set(columns) - set(roles.get(role, ())))
                for role, columns in gold.roles
            )
            return sketch_distance(gold, candidate.profile), schema_gap, candidate.rank, candidate.sql

        candidate = min(candidates, key=key)
        missing, extra = sketch_delta(gold, candidate.profile)
        nearest = {
            "rank": candidate.rank,
            "sql": candidate.sql,
            "sketch_distance": sketch_distance(gold, candidate.profile),
            "missing_sketch_features": missing,
            "extra_sketch_features": extra,
            "profile": candidate.profile.to_dict(),
        }

    return {
        "status": status,
        "bottleneck": bottleneck,
        "candidate_count": len(candidates),
        "executable_count": len(executable),
        "strict_ranks": strict_ranks,
        "lenient_ranks": lenient_ranks,
        "pool_sketch_covered": bool(sketch_matches),
        "pool_table_covered": not missing_tables,
        "pool_role_columns_covered": not missing_roles,
        "candidate_schema_covered": bool(schema_matches),
        "combined_profile_covered": bool(combined_matches),
        "missing_sketch_features": missing_sketch,
        "missing_tables": missing_tables,
        "missing_role_columns": missing_roles,
        "nearest": nearest,
    }
