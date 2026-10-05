"""Bounded natural-language decomposition fused through the one typed-AST planner."""

from __future__ import annotations

import json
import keyword
import re
from collections import Counter
from dataclasses import dataclass, replace
from itertools import chain
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


def single_branch(candidate) -> bool:
    """Whether one dual-emitter branch can represent the candidate: a typed SELECT."""
    from engine.sql_ast import SelectQuery

    return isinstance(candidate.query, SelectQuery)


def selected_decomposition_required(candidate) -> dict[str, Any] | None:
    """One selected-AST predicate, shared by the delegate and compose probe."""
    if candidate is not None and not single_branch(candidate):
        return {
            "reason": "the selected typed AST is compound and cannot be represented by one dual-emitter branch"
        }
    return None


def compound_candidate(searched):
    """The search's compound reading that makes a named request need decomposition, or None.

    Compound structure is the deterministic search's own reading of the question: the top
    candidate of its pool (``search_pool``), from a grammar that models set operations, read before
    any candidate runs. A single-query answer to a multi-goal question is only a fragment of it; the
    decomposition proposal owns such questions.
    """
    top = searched[0] if searched else None
    return top if selected_decomposition_required(top) is not None else None


def ranked_measure_projected(candidate):
    """The same ranking with its ORDER BY aggregates projected, or None when none is hidden.

    A names-only ranking ("top 2 categories by revenue" read as ``category ... ORDER BY
    SUM(line_total)``) is the right reading with its measure hidden, and merges and the final
    ordering need that measure. Projecting an aggregate the ORDER BY already computes changes
    no row and no order, so this is the same query, not another reading.
    """
    from engine.sql_ast import (
        Aggregate,
        ASTValidationError,
        SelectItem,
        SelectQuery,
        render_query,
        validate_query,
    )
    from engine.sql_candidate import ScoredQuery

    query = candidate.query
    if not isinstance(query, SelectQuery) or not query.group_by:
        return None
    shown = {item.expression for item in query.select}
    hidden = tuple(dict.fromkeys(
        term.expression for term in query.order_by
        if isinstance(term.expression, Aggregate) and term.expression not in shown
    ))
    if not hidden:
        return None
    projected = replace(query, select=query.select + tuple(SelectItem(measure) for measure in hidden))
    try:
        validate_query(projected)
    except ASTValidationError:
        return None
    return ScoredQuery(projected, render_query(projected), candidate.score,
                       candidate.evidence + ("leaf:ranked-measure-projected",))


def ranked_entity_projected(candidate, node_id: str, question: str, feeds_cross: bool):
    """Project an over-detailed grouped ranking to its explicitly named entity.

    A cross-input ranking must represent one ranked entity plus its measure. If the typed
    proposal groups a named category by both category and product, the leaf wording can
    identify the requested head noun; dropping that extra group key computes the requested
    category total rather than changing the merge grain. The rewrite is deliberately narrow:
    exactly one group key must match the ranking head noun, the query must be a limited
    aggregate ranking, and its projection/order must remain representable after projection.
    """
    from engine.sql_ast import (
        Aggregate,
        ASTValidationError,
        ColumnRef,
        SelectQuery,
        render_query,
        validate_query,
    )
    from engine.sql_candidate import ScoredQuery
    from engine.sql_expansion import name_tokens, tokens

    query = candidate.query
    if (not feeds_cross or not isinstance(query, SelectQuery) or query.having is not None
            or query.limit is None or len(query.group_by) <= 1
            or not any(isinstance(term.expression, Aggregate) for term in query.order_by)):
        return None
    words = list(tokens(question))
    rank_at = next((index for index, word in enumerate(words)
                    if word in {"top", "bottom", "highest", "lowest", "most", "least"}), None)
    if rank_at is None:
        return None
    by_at = next((index for index in range(rank_at + 1, len(words))
                  if words[index] == "by"), None)
    if by_at is None:
        return None
    entity_words = words[rank_at + 1:by_at]
    while entity_words and (entity_words[0].isdigit()
                            or entity_words[0] in {"the", "a", "an"}):
        entity_words.pop(0)
    if any(word in {"and", "or"} for word in entity_words):
        return None
    if entity_words and entity_words[-1] in {"name", "names", "entity", "entities"}:
        entity_words.pop()
    if not entity_words:
        return None
    head = entity_words[-1]
    matches = [
        group for group in query.group_by
        if isinstance(group, ColumnRef) and head in name_tokens(group.name)
    ]
    if len(matches) != 1:
        return None
    entity = matches[0]
    # Do not silently change an ordering or projection that depends on the discarded key.
    if any(
        not isinstance(term.expression, Aggregate) and term.expression != entity
        for term in query.order_by
    ):
        return None
    ranking_measures = {
        term.expression for term in query.order_by
        if isinstance(term.expression, Aggregate)
    }
    projected_items = tuple(
        item for item in query.select
        if item.expression == entity or item.expression in ranking_measures
    )
    if not any(item.expression == entity for item in projected_items):
        return None
    projected = replace(query, select=projected_items, group_by=(entity,))
    try:
        validate_query(projected)
    except ASTValidationError:
        return None
    return ScoredQuery(
        projected,
        render_query(projected),
        candidate.score,
        candidate.evidence + (f"leaf:ranked-entity-projected:{node_id}:{entity.name}",),
    )


def leaf_candidates(selection, node_id: str, question: str, feeds_cross: bool):
    """Return all single-query readings in selection order, with named measures projected.

    Readings follow the selection's order, with its choice first and each names-only ranking
    projected with its measure. The caller must enforce the measure and ranking-grain
    contracts separately: a lower-ranked pool member may be compatible even when the
    preferred reading is not.
    """
    order = [selection.selected] if selection.selected is not None else []
    order += [index for index in selection.ranking if index != selection.selected]
    readings = []
    seen_sql = set()
    for index in order:
        candidate = selection.pool[index]
        if not single_branch(candidate):
            continue
        reading = ranked_measure_projected(candidate) or candidate
        for option in (reading, ranked_entity_projected(
                reading, node_id, question, feeds_cross)):
            if option is not None and option.sql not in seen_sql:
                seen_sql.add(option.sql)
                readings.append(option)
    return readings


@dataclass(frozen=True)
class SearchProbe:
    """The search's reading of a question over a request's tables, without execution (``search_probe``):
    whether it is compound, its pool, and the schema graph that pool was searched on."""
    decomposition_required: dict[str, Any] | None
    pool: tuple
    graph: Any


def search_probe(planner, tables, question) -> SearchProbe:
    """Execution-free probe: does the search read the question as compound, and how?

    Compound is the same predicate the own-data serve path applies (``compound_candidate``: the
    search's top candidate is a set operation). The compose path must consult it BEFORE
    building a composition: a multi-goal question's surface ("top ...") can satisfy the
    compose gate, and a composed top-N would then answer one fragment of the question.
    Compound structure is the search's reading alone, so the probe runs only the search stage
    of the one own-data selection (``search_pool``): no execution, no fallback. A
    failed probe must not authorize a partial composed answer. Its pool also tells routing
    whether the upload reads the question whole (``engine.routing.reads_upload_whole``).
    """
    from engine.sql_schema import SchemaGraph

    try:
        norm, inferred_fks = planner.ingest(tables)
        schema, _, _ = planner.schema(norm, inferred_fks)
        graph = SchemaGraph.from_planner(schema, inferred_fks)
        searched = tuple(planner.search_pool(question, norm, inferred_fks, schema, graph=graph))
        return SearchProbe(selected_decomposition_required(compound_candidate(searched)), searched, graph)
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
    """Model-facing rejection when a leaf does not sum, or rank by, the measure it names.

    A leaf such as "top 3 products by units sold" names a transactional measure that must be
    summed. When the candidate pool proves the named measure can be summed, a plan that sums
    another column (units where the question says spend) or nothing, or a ranking ordered by
    anything but that measure, would return a confident wrong answer; the proposal is rejected
    instead so the proposer can restate the leaf with the aggregation explicit.
    """
    from engine.sql_ast import Aggregate, SelectQuery
    from engine.sql_expansion import implicit_sum_measures, name_tokens, tokens

    measures = implicit_sum_measures(tokens(question))
    if not measures:
        return None
    wanted = frozenset().union(*(measure.column_words for measure in measures))

    def named(expression) -> bool:
        name = getattr(getattr(expression, "operand", None), "name", None)
        return (isinstance(expression, Aggregate) and name is not None
                and bool(set(name_tokens(name)) & wanted))

    def sums_named(query) -> bool:
        return any(named(item.expression) for item in query.select)

    if not any(isinstance(getattr(candidate, "query", None), SelectQuery)
               and sums_named(candidate.query) for candidate in pool):
        return None
    if not sums_named(selected):
        return (
            f"subquestion {node_id!r} names a summed measure but its selected "
            "plan does not aggregate it; restate this subquestion with the "
            "aggregation explicit (for example 'total quantity sold' or "
            "'total revenue')"
        )
    if selected.limit is not None and selected.order_by and not any(
            named(term.expression) for term in selected.order_by):
        return (
            f"subquestion {node_id!r} ranks by something other than the summed "
            "measure it names; restate it as a ranking by that total"
        )
    return None


def unstated_cutoff_rejection(node_id: str, question: str, limit: int | None) -> str | None:
    """Model-facing rejection when a leaf keeps a row cutoff the question never states.

    A cross merge needs explicit limits on both inputs, and a proposer told so will invent
    them. The Chrome pass of 2026-09-24 answered "products no customer from Paris has bought"
    by crossing "the top 100 customer names from Paris" with "the top 100 product names": a
    question nobody asked. A leaf may keep only a cutoff that the decomposed question states,
    in digits or in words. One row is the planner's reading of a singular superlative ("the
    best-selling product") and needs no number.
    """
    from engine.sql_expansion import parse_number, tokens

    if limit is None or limit == 1:
        return None
    if limit in {parse_number(token) for token in tokens(question)}:
        return None
    return (
        f"subquestion {node_id!r} keeps only {limit} rows, a cutoff the question does not "
        "state; a subquestion may keep only the cutoffs the question names, so a question "
        "that names none is not answered by crossing two lists"
    )


def build_decomposed_plan(
    planner,
    slug: str,
    tables: list[dict],
    schema: list[dict],
    foreign_keys: list[dict] | tuple[dict, ...],
    proposal: dict[str, Any],
    *,
    question: str,
) -> AnalysisPlan:
    """Plan every leaf normally, then fuse their typed outputs with validated merges.

    `question` is the question the proposal decomposes: a leaf may keep only a row cutoff it
    states (`unstated_cutoff_rejection`).

    An anti-join's evidence leaf is chosen when its merge is built: the first of its
    contract-compatible readings, in selection order, that keeps every dimension of the left
    input (`_bind_merge_keys`). "For each customer, list every product name they have ever
    bought" was ranked first as a product-only reading, which cannot say which customer bought
    what; the customer-and-product reading ranked next is the evidence the merge needs.
    """
    from engine.analysis import analysis_view_name
    from engine.deterministic.plan import (
        AnalysisPlan,
        AntiJoinView,
        CrossView,
        PlanSection,
    )

    proposal = validate_decomposition(proposal)
    if proposal is None:  # pragma: no cover - callers require a proposal
        raise DecompositionError("decomposition is required")
    cross_inputs = {
        node_id
        for merge in proposal["merges"] if merge["op"] == "cross"
        for node_id in merge["inputs"]
    }
    uses = Counter(source for merge in proposal["merges"] for source in merge["inputs"])
    leaf_ids = {node["id"] for node in proposal["subquestions"]}
    evidence = {
        merge["inputs"][1] for merge in proposal["merges"]
        if merge["op"] == "anti_join" and merge["inputs"][1] in leaf_ids
        and uses[merge["inputs"][1]] == 1
    }
    tables_by_name: dict[str, TableSpec] = {}
    leaf_views: dict[str, tuple] = {}
    logical_names: dict[str, tuple] = {}
    merge_views = []
    merge_sections = []
    outputs: dict[str, str] = {}
    row_bounds: dict[str, int | None] = {}
    pending = {}

    def fused(trial=()):
        """Every committed leaf view in subquestion order, a trial leaf's, then the merges."""
        return tuple(
            view for node in proposal["subquestions"] for view in leaf_views.get(node["id"], ())
        ) + tuple(trial) + tuple(merge_views)

    def with_tables(child):
        merged = dict(tables_by_name)
        for table in child.tables:
            merged[table.name] = _merge_table(merged.get(table.name), table)
        return merged

    def commit(node_id, child, merged):
        tables_by_name.clear()
        tables_by_name.update(merged)
        leaf_views[node_id] = child.views
        logical_names[node_id] = child.logical_names
        outputs[node_id] = str(child.output)
        row_bounds[node_id] = _row_bound(child, str(child.output))

    tablemap = {table["name"]: table for table in tables}
    for node in proposal["subquestions"]:
        # Reject a proposed cutoff before leaf selection can hide its violation
        # behind an unrelated unsupported-plan error.
        from engine.sql_expansion import parse_number, tokens
        words = tokens(node['question'])
        for cue, number in zip(words, words[1:]):
            if cue in {'top', 'bottom'}:
                cutoff = parse_number(number)
                if cutoff is not None:
                    rejection = unstated_cutoff_rejection(node['id'], question, cutoff)
                    if rejection:
                        raise DecompositionError(rejection)
        readings = _leaf_readings(
            planner, slug, node, tables, schema, foreign_keys, tablemap,
            feeds_cross=node["id"] in cross_inputs, question=question,
        )
        first = next(readings)
        if node["id"] in evidence:
            pending[node["id"]] = (first, readings)
        else:
            commit(node["id"], first, with_tables(first))
    _shapes(fused(), tuple(tables_by_name.values()), slug)

    for merge in proposal["merges"]:
        left_id, right_id = merge["inputs"]
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
                    "cross inputs require explicit limits, stated by the question, whose "
                    "product is at most 10,000 rows"
                )
            view = CrossView(name, outputs[left_id], outputs[right_id], right_prefix=f"{right_id}_")
            row_bounds[merge["id"]] = left_bound * right_bound
        else:
            left = outputs[left_id]
            if right_id in pending:
                first, rest = pending.pop(right_id)
                keys = None
                first_failure = None
                for child in chain((first,), rest):
                    try:
                        merged = with_tables(child)
                        trial = fused(child.views)
                        keys = _bind_merge_keys(
                            trial, _shapes(trial, tuple(merged.values()), slug),
                            left, str(child.output), foreign_keys,
                        )
                    except DecompositionError as exc:
                        if first_failure is None:
                            first_failure = exc
                        continue
                    commit(right_id, child, merged)
                    break
                if keys is None:
                    raise first_failure
            else:
                current = fused()
                keys = _bind_merge_keys(
                    current, _shapes(current, tuple(tables_by_name.values()), slug),
                    left, outputs[right_id], foreign_keys,
                )
            view = AntiJoinView(
                name,
                left,
                outputs[right_id],
                keys,
                _inherited_order(fused(), left),
            )
            row_bounds[merge["id"]] = row_bounds[left_id]
        merge_views.append(view)
        logical_names[merge["id"]] = ((name, merge["id"]),)
        outputs[merge["id"]] = name
        merge_sections.append(
            PlanSection(
                merge["id"],
                merge["label"],
                merge["label"],
                (name,),
                merge["inputs"],
            )
        )
        _shapes(fused(), tuple(tables_by_name.values()), slug)

    views = fused()
    shapes = _shapes(views, tuple(tables_by_name.values()), slug)
    repeated = duplicated_output_dimension(views, shapes, outputs[proposal["output"]])
    if repeated is not None:
        raise DecompositionError(repeated)

    sections = [
        PlanSection(
            node["id"],
            node["label"],
            node["question"],
            tuple(view.name for view in leaf_views[node["id"]]),
        )
        for node in proposal["subquestions"]
    ]
    return AnalysisPlan(
        slug,
        tuple(tables_by_name.values()),
        views,
        outputs[proposal["output"]],
        tuple(sections + merge_sections),
        tuple(pair for node_id in [*(node["id"] for node in proposal["subquestions"]),
                                   *(merge["id"] for merge in proposal["merges"])]
              for pair in logical_names[node_id]),
    )


def _leaf_readings(planner, slug, node, tables, schema, foreign_keys, tablemap, *,
                   feeds_cross: bool, question: str):
    """Yield a leaf's contract-compatible readings, lowered and named for the root slug.

    Readings come in the selection's order (`leaf_candidates`). Raises DecompositionError,
    naming the first reason a reading failed, when the leaf has none.

    A leaf never takes the Gemini fallback: its question is already the chat model's wording, and a
    leaf with no runnable query rejects the decomposition with that reason so the model restates
    the leaf (a bounded correction), instead of fusing an unlabelled Gemini reading into the answer.
    """
    from engine.analysis import analysis_view_name
    from engine.deterministic.lower import (
        UnsupportedDeterministicPlan,
        lower_select_query,
    )

    selection = planner.select_query(
        node["question"], tables, foreign_keys, schema, tablemap, allow_fallback=False
    )
    candidates = selection.pool
    if not candidates:
        raise DecompositionError(
            f"subquestion {node['id']!r} produced no typed AST candidate"
        )
    options = leaf_candidates(selection, node["id"], node["question"], feeds_cross)
    if not options:
        raise DecompositionError(
            f"subquestion {node['id']!r} requires an unsupported compound query"
        )
    first_rejection = None
    first_guard_failure = None
    first_lowering_failure = None
    found = False
    # Pool order is the selection's order, but compatibility with the typed AST does
    # not imply compatibility with the stricter dual-emitter subset. Try each
    # already-ranked, contract-compatible reading before clarifying. This preserves
    # all semantic and deterministic guards while avoiding a false failure caused
    # solely by an unsupported top-ranked representation.
    for option in options:
        ok, reason = planner.guard(option.sql)
        if not ok:
            if first_guard_failure is None:
                first_guard_failure = reason
            continue
        rejection = leaf_measure_rejection(
            node["id"], node["question"], option.query, candidates
        ) or ranked_leaf_grain_rejection(
            node["id"], option.query, feeds_cross
        ) or unstated_cutoff_rejection(
            node["id"], question, option.query.limit
        )
        if rejection is not None:
            if first_rejection is None:
                first_rejection = rejection
            continue
        try:
            child = lower_select_query(
                node["id"],
                option.query,
                schema,
                foreign_keys,
                postgres_row_identity=getattr(planner, "postgres_row_identity", False),
                # A natural-language leaf such as "for each purchase" can be selected
                # as a typed SELECT * even when its downstream merge only needs named
                # dimensions. Normalize that AST before either emitter is built.
                expand_stars=True,
            )
        except UnsupportedDeterministicPlan as exc:
            if first_lowering_failure is None:
                first_lowering_failure = exc
            continue
        found = True
        # Every leaf is linear, but its names must use the ROOT slug's 63-byte
        # naming contract. PostgreSQL truncates long identifiers silently; raw
        # slug + node + stage concatenation can collapse multiple stages to one.
        # A cut name carries a hash, so each view keeps its leaf name as its logical name.
        names = {view.name: analysis_view_name(slug, view.name) for view in child.views}
        yield replace(
            child,
            slug=slug,
            logical_names=tuple((names[view.name], view.name) for view in child.views),
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
    if found:
        return
    if first_rejection is not None:
        raise DecompositionError(first_rejection)
    if first_guard_failure is not None:
        raise DecompositionError(
            f"subquestion {node['id']!r} failed the query guard: "
            f"{first_guard_failure}"
        )
    if first_lowering_failure is not None:
        raise DecompositionError(
            f"subquestion {node['id']!r} cannot use both emitters: "
            f"{first_lowering_failure}"
        ) from first_lowering_failure
    raise DecompositionError(
        f"subquestion {node['id']!r} has no candidate satisfying the leaf contract"
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


def duplicated_output_dimension(views, shapes, output: str) -> str | None:
    """Reject an answer whose grain repeats one physical dimension.

    Crossing two leaves that describe the SAME entity produces an output with two
    columns of identical lineage: every row then pairs a value with itself, and the
    table reads as a confident answer while being a modelling error. The closed
    grammar crosses DIFFERENT entities (customers x products), so repeated lineage
    is never a supported grain. Aggregate aliases carry no lineage and are exempt,
    so two measures that share a name cannot trip this.
    """
    seen: dict[tuple[str, str], str] = {}
    for name, origin in _merge_key_columns(views, shapes, output).items():
        if origin in seen:
            return (
                f"the combined answer would repeat the {origin[0]}.{origin[1]} column "
                f"as both {seen[origin]!r} and {name!r}; cross two subquestions about "
                "DIFFERENT entities, or use one subquestion when only one is needed"
            )
        seen[origin] = name
    return None


def _bind_merge_keys(views, shapes, left, right, foreign_keys=()):
    """Bind aliases only when physical lineage matches or a direct FK proves identity.

    Ambiguous duplicate projections and unrelated columns sharing an alias fail
    closed. A direct, single-column inclusion dependency is sufficient to compare
    the same named entity key across its source and referenced table; composite or
    partial dependencies are deliberately not treated as identity.
    """
    from engine.deterministic.plan import MergeKey

    def equivalent(left_origin, right_origin):
        if left_origin == right_origin:
            return True
        lt, lc = left_origin
        rt, rc = right_origin
        return any(
            edge.get("from_table") == lt
            and edge.get("from_col") == lc
            and edge.get("to_table") == rt
            and edge.get("to_col") == rc
            and float(edge.get("inclusion", 0.0)) == 1.0
            for edge in foreign_keys
        ) or any(
            edge.get("from_table") == rt
            and edge.get("from_col") == rc
            and edge.get("to_table") == lt
            and edge.get("to_col") == lc
            and float(edge.get("inclusion", 0.0)) == 1.0
            for edge in foreign_keys
        )

    left_keys = _merge_key_columns(views, shapes, left)
    right_keys = _merge_key_columns(views, shapes, right)
    if any(
        not equivalent(left_keys[name], right_keys[name])
        for name in left_keys.keys() & right_keys.keys()
    ):
        raise DecompositionError(
            "anti-join aliases refer to different dimension lineage"
        )
    keys = []
    missing = []
    for column, origin in left_keys.items():
        matches = [name for name, other in right_keys.items() if equivalent(origin, other)]
        if len(matches) > 1 or (matches and list(left_keys.values()).count(origin) > 1):
            raise DecompositionError("anti-join dimension binding is ambiguous")
        if matches:
            keys.append(MergeKey(column, matches[0]))
        else:
            missing.append(column)
    if missing:
        quoted = ", ".join(repr(column) for column in missing)
        raise DecompositionError(
            "the anti-join evidence is at a coarser grain than its left input; "
            f"its subquestion must also preserve the left-side dimension(s) {quoted}"
        )
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
    if len(value) < 3:
        # A node id names the sheets and sections the user reads: leaves named "c", "p" and "ev"
        # became the sheets "c combined" and "ev result" and the column "p_sum" (Chrome gate,
        # 2026-10-02). The model renames it within its correction budget.
        raise DecompositionError(
            f"{label} {value!r} names the sheets the user reads: use a readable snake_case name "
            "of what it holds, such as top_customers"
        )
    return value


def _text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DecompositionError(f"{label} is required")
    value = value.strip()
    if len(value) > limit:
        raise DecompositionError(f"{label} is too long")
    return value
