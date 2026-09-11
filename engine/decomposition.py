"""Bounded natural-language decomposition fused through the one typed-AST planner."""

from __future__ import annotations

import json
import keyword
from dataclasses import replace
from typing import Any

from engine.analysis import analysis_view_name
from engine.deterministic.lower import UnsupportedDeterministicPlan, lower_select_query
from engine.deterministic.plan import (
    AnalysisPlan,
    AntiJoinView,
    CrossView,
    MergeKey,
    PlanSection,
    ProjectedView,
    ReducedView,
    RelationshipSpec,
    SortedView,
    SortValue,
    TableSpec,
    ViewValue,
)
from engine.sql_ast import SelectQuery

MAX_SUBQUESTIONS = 4
MAX_MERGES = 4
MAX_DECOMPOSITION_BYTES = 12_000
MAX_INTERMEDIATE_ROWS = 10_000
_MERGE_OPS = frozenset({"cross", "anti_join"})


class DecompositionError(ValueError):
    """A proposal is malformed, unsafe, ambiguous, or cannot be lowered."""


def validate_decomposition(value: object) -> dict[str, Any] | None:
    """Validate the model-facing closed grammar without binding any schema object."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DecompositionError("decomposition must be an object")
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":"))) > MAX_DECOMPOSITION_BYTES:
        raise DecompositionError("decomposition is too large")
    unknown = set(value) - {"subquestions", "merges", "output", "grain"}
    if unknown:
        raise DecompositionError(
            "decomposition contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    raw_questions = value.get("subquestions")
    raw_merges = value.get("merges")
    if not isinstance(raw_questions, list) or not 2 <= len(raw_questions) <= MAX_SUBQUESTIONS:
        raise DecompositionError("decomposition requires two to four subquestions")
    if not isinstance(raw_merges, list) or not 1 <= len(raw_merges) <= MAX_MERGES:
        raise DecompositionError("decomposition requires one to four merge steps")

    nodes: set[str] = set()
    subquestions = []
    for item in raw_questions:
        if not isinstance(item, dict) or set(item) - {"id", "question", "label"}:
            raise DecompositionError("each subquestion has id, question, and optional label")
        node_id = _identifier(item.get("id"), "subquestion id")
        question = _text(item.get("question"), "subquestion", 600)
        label = _text(item.get("label") or node_id.replace("_", " "), "subquestion label", 80)
        if node_id in nodes:
            raise DecompositionError("decomposition node ids must be unique")
        nodes.add(node_id)
        subquestions.append({"id": node_id, "question": question, "label": label})

    merges = []
    for item in raw_merges:
        if not isinstance(item, dict) or set(item) - {"id", "op", "inputs", "label"}:
            raise DecompositionError("each merge has id, op, inputs, and optional label")
        node_id = _identifier(item.get("id"), "merge id")
        op = item.get("op")
        inputs = item.get("inputs")
        if op not in _MERGE_OPS:
            raise DecompositionError("merge op must be cross or anti_join")
        if not isinstance(inputs, list) or len(inputs) != 2:
            raise DecompositionError("each merge requires exactly two inputs")
        normalized_inputs = tuple(_identifier(source, "merge input") for source in inputs)
        if normalized_inputs[0] == normalized_inputs[1] or any(source not in nodes for source in normalized_inputs):
            raise DecompositionError("merge inputs must name two preceding nodes")
        if node_id in nodes:
            raise DecompositionError("decomposition node ids must be unique")
        label = _text(item.get("label") or node_id.replace("_", " "), "merge label", 80)
        nodes.add(node_id)
        merges.append(
            {"id": node_id, "op": op, "inputs": normalized_inputs, "label": label}
        )

    output = _identifier(value.get("output"), "decomposition output")
    grain = _text(value.get("grain"), "output grain", 120)
    if output not in {item["id"] for item in merges}:
        raise DecompositionError("decomposition output must name a merge node")
    dependencies = {
        item["id"]: tuple(item["inputs"])
        for item in merges
    }
    reachable: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in reachable:
            return
        reachable.add(node_id)
        for source in dependencies.get(node_id, ()):
            visit(source)

    visit(output)
    if reachable != nodes:
        raise DecompositionError(
            "every decomposition node must contribute to the requested output"
        )
    return {
        "subquestions": subquestions,
        "merges": merges,
        "output": output,
        "grain": grain,
    }


def build_decomposed_plan(
    planner,
    slug: str,
    tables: list[dict],
    schema: list[dict],
    foreign_keys: list[dict] | tuple[dict, ...],
    proposal: dict[str, Any],
) -> AnalysisPlan:
    """Plan every leaf normally, then fuse their typed outputs with validated merges."""
    from engine.calculations import select_calculation_candidate
    from engine.sql_schema import SchemaGraph

    proposal = validate_decomposition(proposal)
    if proposal is None:  # pragma: no cover - callers require a proposal
        raise DecompositionError("decomposition is required")
    graph = SchemaGraph.from_planner(schema, foreign_keys)
    views = []
    sections = []
    tables_by_name: dict[str, TableSpec] = {}
    outputs: dict[str, str] = {}
    row_bounds: dict[str, int | None] = {}

    for node in proposal["subquestions"]:
        candidates = planner.search_ast(
            node["question"], schema, tables, foreign_keys, max_candidates=25
        )
        if not candidates:
            raise DecompositionError(
                f"subquestion {node['id']!r} produced no typed AST candidate"
            )
        candidate, _, _ = select_calculation_candidate(
            node["question"], tables, graph, candidates
        )
        if not isinstance(candidate.query, SelectQuery):
            raise DecompositionError(
                f"subquestion {node['id']!r} requires an unsupported compound query"
            )
        ok, reason = planner.guard(candidate.sql)
        if not ok:
            raise DecompositionError(
                f"subquestion {node['id']!r} failed the query guard: {reason}"
            )
        try:
            child = lower_select_query(
                f"{slug}_{node['id']}",
                candidate.query,
                schema,
                foreign_keys,
                postgres_row_identity=getattr(planner, "postgres_row_identity", False),
            )
        except UnsupportedDeterministicPlan as exc:
            raise DecompositionError(
                f"subquestion {node['id']!r} cannot use both emitters: {exc}"
            ) from exc
        for table in child.tables:
            tables_by_name[table.name] = _merge_table(tables_by_name.get(table.name), table)
        branch_views = tuple(view.name for view in child.views)
        views.extend(child.views)
        outputs[node["id"]] = str(child.output)
        row_bounds[node["id"]] = _row_bound(child, str(child.output))
        sections.append(
            PlanSection(
                node["id"], node["label"], node["question"], branch_views
            )
        )

    shapes = _shapes(tuple(views), tuple(tables_by_name.values()), slug)
    for merge in proposal["merges"]:
        left_id, right_id = merge["inputs"]
        left, right = outputs[left_id], outputs[right_id]
        name = analysis_view_name(slug, merge["id"])
        if merge["op"] == "cross":
            left_bound = row_bounds[left_id]
            right_bound = row_bounds[right_id]
            if left_bound is None or right_bound is None or left_bound * right_bound > MAX_INTERMEDIATE_ROWS:
                raise DecompositionError(
                    "cross inputs require explicit limits whose product is at most 10,000 rows"
                )
            view = CrossView(name, left, right, right_prefix=f"{right_id}_")
            row_bounds[merge["id"]] = left_bound * right_bound
        else:
            left_keys = _merge_key_columns(views, shapes, left)
            right_keys = set(_merge_key_columns(views, shapes, right))
            shared = tuple(column for column in left_keys if column in right_keys)
            if not shared:
                raise DecompositionError(
                    "anti-join inputs have no planner-bound dimension columns in common"
                )
            view = AntiJoinView(
                name,
                left,
                right,
                tuple(MergeKey(column, column) for column in shared),
                _inherited_order(views, left),
            )
            row_bounds[merge["id"]] = row_bounds[left_id]
        views.append(view)
        outputs[merge["id"]] = name
        sections.append(
            PlanSection(
                merge["id"],
                merge["label"],
                merge["label"],
                (name,),
                merge["inputs"],
            )
        )
        shapes = _shapes(tuple(views), tuple(tables_by_name.values()), slug)

    return AnalysisPlan(
        slug,
        tuple(tables_by_name.values()),
        tuple(views),
        outputs[proposal["output"]],
        tuple(sections),
    )


def _merge_table(existing: TableSpec | None, incoming: TableSpec) -> TableSpec:
    if existing is None:
        return incoming
    if (
        existing.class_name != incoming.class_name
        or existing.attribute != incoming.attribute
        or existing.schema != incoming.schema
        or existing.columns != incoming.columns
    ):
        raise DecompositionError(f"subplans disagree about table {incoming.name!r}")
    relationships: list[RelationshipSpec] = list(existing.relationships)
    for relationship in incoming.relationships:
        if relationship not in relationships:
            if any(item.attribute == relationship.attribute for item in relationships):
                raise DecompositionError(
                    f"subplans disagree about relationship {incoming.name}.{relationship.attribute}"
                )
            relationships.append(relationship)
    return replace(existing, relationships=tuple(relationships))


def _shapes(views, tables, slug) -> dict[str, tuple[str, ...]]:
    # Build through the same plan validator/shape calculator used by both emitters.
    return AnalysisPlan(slug, tables, views).view_columns()


def _row_bound(plan: AnalysisPlan | None, output: str) -> int | None:
    if plan is None:
        return None
    view = next(item for item in plan.views if item.name == output)
    return view.limit if isinstance(view, SortedView) else None


def _inherited_order(views, source: str) -> tuple[SortValue, ...]:
    view = next(item for item in views if item.name == source)
    if isinstance(view, SortedView):
        return view.order
    if isinstance(view, AntiJoinView):
        return view.order
    if isinstance(view, CrossView):
        order = list(_inherited_order(views, view.left))
        right_order = _inherited_order(views, view.right)
        if right_order:
            left_shape = _output_names(views, view.left)
            order.extend(
                SortValue(
                    ViewValue(
                        view.right_prefix + item.value.name
                        if isinstance(item.value, ViewValue) and item.value.name in left_shape
                        else item.value.name
                    ),
                    item.descending,
                )
                for item in right_order
                if isinstance(item.value, ViewValue)
            )
        return tuple(order)
    return ()


def _merge_key_columns(views, shapes, name: str) -> tuple[str, ...]:
    """Return only planner-bound dimensions that are safe equality merge keys.

    Aggregate aliases are deliberately excluded. Treating two identically named
    measures as identity columns can silently over-constrain an anti-join.
    """
    view = next(item for item in views if item.name == name)
    if isinstance(view, SortedView):
        return _merge_key_columns(views, shapes, view.source)
    if isinstance(view, ProjectedView):
        return tuple(item.name for item in view.values)
    if isinstance(view, ReducedView):
        return tuple(item.name for item in view.group_by)
    if isinstance(view, CrossView):
        left = _merge_key_columns(views, shapes, view.left)
        left_output = set(shapes[view.left])
        right = tuple(
            view.right_prefix + column if column in left_output else column
            for column in _merge_key_columns(views, shapes, view.right)
        )
        return tuple(dict.fromkeys(left + right))
    if isinstance(view, AntiJoinView):
        return _merge_key_columns(views, shapes, view.left)
    return ()


def _output_names(views, name: str) -> set[str]:
    view = next(item for item in views if item.name == name)
    if isinstance(view, SortedView):
        return _output_names(views, view.source)
    if isinstance(view, CrossView):
        left = _output_names(views, view.left)
        return left | {
            view.right_prefix + column if column in left else column
            for column in _output_names(views, view.right)
        }
    if isinstance(view, AntiJoinView):
        return _output_names(views, view.left)
    if hasattr(view, "values"):
        return {item.name for item in view.values}
    if hasattr(view, "group_by"):
        return {item.name for item in view.group_by + view.aggregates}
    return set()


def _identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.isidentifier()
        or keyword.iskeyword(value)
        or value.startswith("__")
    ):
        raise DecompositionError(f"{label} must be a short identifier")
    if len(value) > 40:
        raise DecompositionError(f"{label} is too long")
    return value


def _text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecompositionError(f"{label} is required")
    value = value.strip()
    if len(value) > limit:
        raise DecompositionError(f"{label} is too long")
    return value
