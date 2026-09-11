"""Bounded natural-language decomposition fused through the one typed-AST planner."""

from __future__ import annotations

import json
import keyword
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from engine.deterministic.plan import AnalysisPlan, TableSpec

# The planner/plan imports stay INSIDE the fusion functions. The lean orchestrator
# image ships this module only for `validate_decomposition` — the pure closed-grammar
# check — and must not drag the typed-AST planner stack into an image that exists to
# exclude it. The engine image is the only caller of `build_decomposed_plan`.

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
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DecompositionError("decomposition must contain JSON values") from exc
    if len(encoded) > MAX_DECOMPOSITION_BYTES:
        raise DecompositionError("decomposition is too large")
    unknown = set(value) - {"subquestions", "merges", "output", "grain"}
    if unknown:
        raise DecompositionError(
            "decomposition contains unsupported fields: " + ", ".join(sorted(unknown))
        )
    raw_questions = value.get("subquestions")
    raw_merges = value.get("merges")
    if (
        not isinstance(raw_questions, list)
        or not 2 <= len(raw_questions) <= MAX_SUBQUESTIONS
    ):
        raise DecompositionError("decomposition requires two to four subquestions")
    if not isinstance(raw_merges, list) or not 1 <= len(raw_merges) <= MAX_MERGES:
        raise DecompositionError("decomposition requires one to four merge steps")

    nodes: set[str] = set()
    subquestions = []
    for item in raw_questions:
        if not isinstance(item, dict) or set(item) - {"id", "question", "label"}:
            raise DecompositionError(
                "each subquestion has id, question, and optional label"
            )
        node_id = _identifier(item.get("id"), "subquestion id")
        question = _text(item.get("question"), "subquestion", 600)
        label = _text(
            item.get("label") or node_id.replace("_", " "), "subquestion label", 80
        )
        if node_id in nodes:
            raise DecompositionError("decomposition node ids must be unique")
        nodes.add(node_id)
        subquestions.append({"id": node_id, "question": question, "label": label})

    merges = []
    for item in raw_merges:
        if not isinstance(item, dict) or set(item) - {"id", "op", "inputs", "label"}:
            raise DecompositionError(
                "each merge has id, op, inputs, and optional label"
            )
        node_id = _identifier(item.get("id"), "merge id")
        op = item.get("op")
        inputs = item.get("inputs")
        if not isinstance(op, str) or op not in _MERGE_OPS:
            raise DecompositionError("merge op must be cross or anti_join")
        if not isinstance(inputs, list) or len(inputs) != 2:
            raise DecompositionError("each merge requires exactly two inputs")
        normalized_inputs = tuple(
            _identifier(source, "merge input") for source in inputs
        )
        if normalized_inputs[0] == normalized_inputs[1] or any(
            source not in nodes for source in normalized_inputs
        ):
            raise DecompositionError("merge inputs must name two preceding nodes")
        if node_id in nodes:
            raise DecompositionError("decomposition node ids must be unique")
        label = _text(item.get("label") or node_id.replace("_", " "), "merge label", 80)
        nodes.add(node_id)
        merges.append(
            {"id": node_id, "op": op, "inputs": list(normalized_inputs), "label": label}
        )

    output = _identifier(value.get("output"), "decomposition output")
    grain = _text(value.get("grain"), "output grain", 120)
    if output not in {item["id"] for item in merges}:
        raise DecompositionError("decomposition output must name a merge node")
    dependencies = {item["id"]: tuple(item["inputs"]) for item in merges}
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


def selected_decomposition_required(candidate) -> dict[str, Any] | None:
    """One selected-AST predicate, shared by the delegate and compose probe."""
    from engine.sql_ast import SelectQuery

    if candidate is not None and not isinstance(candidate.query, SelectQuery):
        return {
            "reason": "the selected typed AST is compound and cannot be represented by one dual-emitter branch"
        }
    return None


def compound_decomposition_required(planner, tables, question) -> dict[str, Any] | None:
    """Execution-free probe: does the planner's SELECTED candidate need decomposition?

    This is the same predicate the own-data serve path applies after selection
    (engine/tables.py sets `decomposition_required` when the winner is compound).
    The compose path must consult it BEFORE building a composition: a multi-goal
    question's surface ("top ...") can satisfy the compose gate, and a composed
    top-N would then answer one fragment of the question. The probe selects but
    never executes. A failed probe must not authorize a partial composed answer.
    """
    from engine.calculations import select_calculation_candidate
    from engine.sql_schema import SchemaGraph

    try:
        norm, inferred_fks = planner.ingest(tables)
        schema, _, _ = planner.schema(norm, inferred_fks)
        candidates = planner.search_ast(
            question, schema, norm, inferred_fks, max_candidates=25
        )
        if not candidates:
            return None
        graph = SchemaGraph.from_planner(schema, inferred_fks)
        candidate, _, _ = select_calculation_candidate(
            question, norm, graph, candidates
        )
        return selected_decomposition_required(candidate)
    except Exception as exc:
        raise DecompositionError(
            "could not check the selected query for decomposition"
        ) from exc


def ranked_leaf_grain_rejection(node_id: str, selected, feeds_cross: bool) -> str | None:
    """Model-facing rejection when a ranked cross input carries an extra dimension.

    A ranking leaf that feeds a cross merge defines one side of a candidate-pair
    space, so it must stay at the ranked entity's grain. Grouping it by a second
    descriptive column ("top 3 categories ... and their products") silently turns
    the ranking into the finer entity's ranking: the pair set may survive the
    anti-join, but the ordering and the displayed measure are the wrong grain.
    """
    from engine.sql_ast import Aggregate

    if not feeds_cross or selected.limit is None or len(selected.group_by) <= 1:
        return None
    if not any(isinstance(term.expression, Aggregate) for term in selected.order_by):
        return None
    return (
        f"subquestion {node_id!r} is a ranking but groups {len(selected.group_by)} "
        "columns; a ranking leaf that feeds a cross merge must name only the ranked "
        "entity and its measure — drop the extra descriptive column and keep the "
        "detail in the evidence subquestion instead"
    )


def leaf_measure_rejection(node_id: str, question: str, selected, pool) -> str | None:
    """Model-facing rejection when a leaf's summed measure lost its aggregation.

    A leaf such as "top 3 products by units sold" names a transactional measure
    that must be summed. If the selected plan carries no aggregate while the
    candidate pool proves an aggregated reading of that measure exists, answering
    would return a confident wrong ranking; the proposal is rejected instead so
    the proposer can restate the leaf with the aggregation explicit.
    """
    from engine.sql_ast import Aggregate, SelectQuery
    from engine.sql_expansion import implicit_sum_measures, name_tokens, tokens

    measures = implicit_sum_measures(tokens(question))
    if not measures:
        return None

    def aggregates(query: SelectQuery):
        return [item.expression for item in query.select
                if isinstance(item.expression, Aggregate)]

    if aggregates(selected):
        return None
    wanted = frozenset().union(*(measure.column_words for measure in measures))
    for candidate in pool:
        query = getattr(candidate, "query", None)
        if not isinstance(query, SelectQuery):
            continue
        for aggregate in aggregates(query):
            name = getattr(aggregate.operand, "name", None)
            if name is not None and set(name_tokens(name)) & wanted:
                return (
                    f"subquestion {node_id!r} names a summed measure but its selected "
                    "plan does not aggregate; restate this subquestion with the "
                    "aggregation explicit (for example 'total quantity sold' or "
                    "'total revenue')"
                )
    return None


def build_decomposed_plan(
    planner,
    slug: str,
    tables: list[dict],
    schema: list[dict],
    foreign_keys: list[dict] | tuple[dict, ...],
    proposal: dict[str, Any],
) -> AnalysisPlan:
    """Plan every leaf normally, then fuse their typed outputs with validated merges."""
    from engine.analysis import analysis_view_name
    from engine.calculations import select_calculation_candidate
    from engine.deterministic.lower import (
        UnsupportedDeterministicPlan,
        lower_select_query,
    )
    from engine.deterministic.plan import (
        AnalysisPlan,
        AntiJoinView,
        CrossView,
        PlanSection,
    )
    from engine.sql_ast import SelectQuery
    from engine.sql_schema import SchemaGraph

    proposal = validate_decomposition(proposal)
    if proposal is None:  # pragma: no cover - callers require a proposal
        raise DecompositionError("decomposition is required")
    graph = SchemaGraph.from_planner(schema, foreign_keys)
    cross_inputs = {
        node_id
        for merge in proposal["merges"] if merge["op"] == "cross"
        for node_id in merge["inputs"]
    }
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
        if candidate is None or not isinstance(candidate.query, SelectQuery):
            raise DecompositionError(
                f"subquestion {node['id']!r} requires an unsupported compound query"
            )
        ok, reason = planner.guard(candidate.sql)
        if not ok:
            raise DecompositionError(
                f"subquestion {node['id']!r} failed the query guard: {reason}"
            )
        rejection = leaf_measure_rejection(
            node["id"], node["question"], candidate.query, candidates
        ) or ranked_leaf_grain_rejection(
            node["id"], candidate.query, node["id"] in cross_inputs
        )
        if rejection is not None:
            raise DecompositionError(rejection)
        try:
            child = lower_select_query(
                node["id"],
                candidate.query,
                schema,
                foreign_keys,
                postgres_row_identity=getattr(planner, "postgres_row_identity", False),
                # A natural-language leaf such as "for each purchase" can be selected
                # as a typed SELECT * even when its downstream merge only needs named
                # dimensions. Normalize that AST before either emitter is built.
                expand_stars=True,
            )
        except UnsupportedDeterministicPlan as exc:
            raise DecompositionError(
                f"subquestion {node['id']!r} cannot use both emitters: {exc}"
            ) from exc
        # Every leaf is linear, but its names must use the ROOT slug's 63-byte
        # naming contract. PostgreSQL truncates long identifiers silently; raw
        # slug + node + stage concatenation can collapse multiple stages to one.
        names = {view.name: analysis_view_name(slug, view.name) for view in child.views}
        child = replace(
            child,
            slug=slug,
            views=tuple(
                replace(
                    view,
                    name=names[view.name],
                    **(
                        {"source": names[view.source]}
                        if hasattr(view, "source")
                        else {}
                    ),
                )
                for view in child.views
            ),
            output=names[child.output],
        )
        for table in child.tables:
            tables_by_name[table.name] = _merge_table(
                tables_by_name.get(table.name), table
            )
        branch_views = tuple(view.name for view in child.views)
        views.extend(child.views)
        outputs[node["id"]] = str(child.output)
        row_bounds[node["id"]] = _row_bound(child, str(child.output))
        sections.append(
            PlanSection(node["id"], node["label"], node["question"], branch_views)
        )

    shapes = _shapes(tuple(views), tuple(tables_by_name.values()), slug)
    for merge in proposal["merges"]:
        left_id, right_id = merge["inputs"]
        left, right = outputs[left_id], outputs[right_id]
        name = analysis_view_name(slug, merge["id"])
        if merge["op"] == "cross":
            left_bound = row_bounds[left_id]
            right_bound = row_bounds[right_id]
            if (
                left_bound is None
                or right_bound is None
                or left_bound * right_bound > MAX_INTERMEDIATE_ROWS
            ):
                raise DecompositionError(
                    "cross inputs require explicit limits whose product is at most 10,000 rows"
                )
            view = CrossView(name, left, right, right_prefix=f"{right_id}_")
            row_bounds[merge["id"]] = left_bound * right_bound
        else:
            keys = _bind_merge_keys(views, shapes, left, right)
            view = AntiJoinView(
                name,
                left,
                right,
                keys,
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


def _merge_table(existing, incoming):
    if existing is None:
        return incoming

    def shape(spec):
        # Everything EXCEPT the identity flag. Two leaves may prove identity on
        # different unique columns of the same table — a standalone scan prefers the
        # id while a join proves its target name column. The fused plan shares one
        # mapped class, joins reference columns explicitly, and either proven key
        # keeps the ORM identity stable, so the first leaf's proof stands.
        return [
            (column.name, column.attribute, column.type, column.nullable)
            for column in spec.columns
        ]

    if (
        existing.class_name != incoming.class_name
        or existing.attribute != incoming.attribute
        or existing.schema != incoming.schema
        or shape(existing) != shape(incoming)
    ):
        raise DecompositionError(f"subplans disagree about table {incoming.name!r}")
    relationships = list(existing.relationships)
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
    from engine.deterministic.plan import AnalysisPlan

    try:
        return AnalysisPlan(slug, tables, views).view_columns()
    except (TypeError, ValueError) as exc:
        raise DecompositionError(f"subplans cannot be fused safely: {exc}") from exc


def _row_bound(plan, output: str) -> int | None:
    from engine.deterministic.plan import SortedView

    if plan is None:
        return None
    view = next(item for item in plan.views if item.name == output)
    return view.limit if isinstance(view, SortedView) else None


def _inherited_order(views, source: str):
    from engine.deterministic.plan import (
        AntiJoinView,
        CrossView,
        SortedView,
        SortValue,
        ViewValue,
    )

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
                        if isinstance(item.value, ViewValue)
                        and item.value.name in left_shape
                        else item.value.name
                    ),
                    item.descending,
                )
                for item in right_order
                if isinstance(item.value, ViewValue)
            )
        return tuple(order)
    return ()


def _merge_key_columns(views, shapes, name: str) -> dict[str, tuple[str, str]]:
    """Return output names with their physical dimension lineage.

    Aggregate aliases are deliberately excluded. Treating two identically named
    measures as identity columns can silently over-constrain an anti-join.
    """
    from engine.deterministic.plan import (
        AntiJoinView,
        CalculatedView,
        ColumnValue,
        CrossView,
        ProjectedView,
        ReducedView,
        SortedView,
        ViewValue,
    )

    view = next(item for item in views if item.name == name)
    if isinstance(view, SortedView):
        return _merge_key_columns(views, shapes, view.source)
    if isinstance(view, (CalculatedView, ProjectedView, ReducedView)):
        prior = _merge_key_columns(views, shapes, view.source)
        values = view.group_by if isinstance(view, ReducedView) else view.values
        result = dict(prior) if isinstance(view, CalculatedView) else {}
        for item in values:
            result.pop(item.name, None)
            if isinstance(item.value, ColumnValue):
                result[item.name] = (item.value.table, item.value.column)
            elif isinstance(item.value, ViewValue) and item.value.name in prior:
                result[item.name] = prior[item.value.name]
        return result
    if isinstance(view, CrossView):
        left = _merge_key_columns(views, shapes, view.left)
        left_output = set(shapes[view.left])
        right = {
            view.right_prefix + column if column in left_output else column: origin
            for column, origin in _merge_key_columns(views, shapes, view.right).items()
        }
        return {**left, **right}
    if isinstance(view, AntiJoinView):
        return _merge_key_columns(views, shapes, view.left)
    if hasattr(view, "source"):
        return _merge_key_columns(views, shapes, view.source)
    return {}


def _bind_merge_keys(views, shapes, left, right):
    """Aliases are presentation, not identity. Bind only identical physical columns.

    Ambiguous duplicate projections and unrelated columns sharing an alias fail
    closed. Foreign-key equivalence needs a separate composite-key proof; merely
    sharing a spelling (or a value) is never sufficient.
    """
    from engine.deterministic.plan import MergeKey

    left_keys = _merge_key_columns(views, shapes, left)
    right_keys = _merge_key_columns(views, shapes, right)
    if any(
        left_keys[name] != right_keys[name]
        for name in left_keys.keys() & right_keys.keys()
    ):
        raise DecompositionError(
            "anti-join aliases refer to different dimension lineage"
        )
    keys = []
    for column, origin in left_keys.items():
        matches = [name for name, other in right_keys.items() if origin == other]
        if len(matches) > 1 or (matches and list(left_keys.values()).count(origin) > 1):
            raise DecompositionError("anti-join dimension binding is ambiguous")
        if matches:
            keys.append(MergeKey(column, matches[0]))
    if not keys:
        raise DecompositionError(
            "anti-join inputs have no common planner-bound dimension lineage"
        )
    return tuple(keys)


def _output_names(views, name: str) -> set[str]:
    from engine.deterministic.plan import AntiJoinView, CrossView, SortedView

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
        or re.fullmatch(r"[a-z][a-z0-9_]{0,39}", value) is None
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
