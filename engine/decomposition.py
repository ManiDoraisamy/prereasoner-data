"""Bounded natural-language decomposition fused through the one own-data selection (engine/tables.py select_query):
each subquestion is a leaf the Decisions path answers, its program's plan fused with the others by validated merges."""

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


# The longest question one query is taken to answer when the Decisions path asks back about it: past it, an asked
# question asks for the decomposition proposal, each subquestion short enough. A longer question the path answers
# is answered: three of the 896 dev questions (31, 32 and 36 words) are, two of them right; the multi-goal prompts
# the shipped complex datasets ask have 39 to 44.
ONE_QUERY_WORDS = 30


def one_query_length(question) -> bool:
    """Whether ``question`` is no longer than one query is taken to answer (``ONE_QUERY_WORDS`` words)."""
    return len(re.findall(r"[^\W_]+", str(question))) <= ONE_QUERY_WORDS


def declined_compound(selection, question) -> dict[str, Any] | None:
    """The decomposition a long question needs, or None.

    A question longer than ``ONE_QUERY_WORDS`` words that the one own-data selection
    (engine/tables.py:TableQuery.select_query) asks back about is read as the bounded subquestions of a decomposition
    proposal instead, each short enough to read; one it answers is answered. A short one the Decisions path asks back
    is asked about (engine/tables.py:unsure_clarification)."""
    if selection is None or selection.selected is not None or one_query_length(question):
        return None
    return {"reason": "a question this long is read as a compound question, never as one query"}


def leaf_candidates(selection):
    """A leaf's readings: (pool index, candidate) of every listed candidate in the selection's ranking, its likeliest
    answer first. A leaf the path would ask back about keeps its readings: a leaf is a step of the plan, not the
    answer, and its contracts below choose among them."""
    return [(index, selection.pool[index]) for index in selection.ranking]


@dataclass(frozen=True)
class SearchProbe:
    """The one own-data selection over a request's tables (``search_probe``): the decomposition a long question needs,
    the selection (None when the question needs one), and the planner's column records it was read on."""
    decomposition_required: dict[str, Any] | None
    selection: Any
    schema: tuple


def search_probe(planner, tables, question) -> SearchProbe:
    """Probe: does the question need a decomposition, and does the upload answer it?

    A question longer than one query answers needs the decomposition proposal (``declined_compound``); the compose
    path consults that BEFORE building a composition, since a multi-goal question's surface ("top ...") can satisfy
    the compose gate and a composed top-N would answer one fragment of it. The one own-data selection
    (``select_query``) answers a shorter one, and its decided reading tells routing that the upload answers the
    question (``engine.routing.reads_upload_whole``); the delegate's selection is the same answer, once per request
    (engine/prepared.py RequestDatabase.answered). A failed probe must not authorize a partial composed answer."""
    try:
        norm, inferred_fks = planner.ingest(tables)
        schema, _, _tablemap = planner.schema(norm, inferred_fks)
        selection = planner.select_query(question, norm)
        required = declined_compound(selection, question)
        return SearchProbe(required, None if required is not None else selection, tuple(schema))
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
    foreign_keys: list[dict] | tuple[dict, ...],
    proposal: dict[str, Any],
    *,
    question: str,
    catalogs: list | None = None,
) -> AnalysisPlan:
    """Plan every leaf normally, then fuse their typed outputs with validated merges.

    `question` is the question the proposal decomposes: a leaf may keep only a row cutoff it
    states (`unstated_cutoff_rejection`). `catalogs`, when given, receives the workbook each
    leaf was read on (`shown_output` restores the sheet's spelling from it).

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
    catalogs = [] if catalogs is None else catalogs
    lineage: dict[tuple[str, str], tuple[str, str]] = {}   # store columns holding another's values (_leaf_readings)

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
            planner, slug, node, tables,
            feeds_cross=node["id"] in cross_inputs, question=question, lineage=lineage, catalogs=catalogs,
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
                            left, str(child.output), foreign_keys, lineage,
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
                    left, outputs[right_id], foreign_keys, lineage,
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
    repeated = duplicated_output_dimension(views, shapes, outputs[proposal["output"]], lineage)
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


def _leaf_readings(planner, slug, node, tables, *, feeds_cross: bool, question: str, lineage=None, catalogs=None):
    """Yield a leaf's contract-compatible readings, lowered and named for the root slug.

    Readings come in the selection's order (`leaf_candidates`), each its program's one plan with its tables declared
    whole, so the leaves' plans over one table fuse (sql_rank.PoolSelection.plan). Raises DecompositionError, naming
    the first reason a reading failed, when the leaf has none: the model restates the leaf (a bounded correction).
    ``lineage`` gains the store columns of the leaf's workbook that hold another column's values
    (engine/program/store.py column_lineage): the request's links connect the tables the leaves read; ``catalogs``
    gains the workbook itself.
    """
    from engine.analysis import analysis_view_name
    from engine.deterministic.lower import UnsupportedDeterministicPlan
    from engine.program.lowering import shown_plan

    selection = planner.select_query(node["question"], tables)
    if selection.catalog is not None:
        from engine.program.store import column_lineage
        if lineage is not None:
            lineage.update(column_lineage(selection.catalog))
        if catalogs is not None:
            catalogs.append(selection.catalog)
    if not selection.pool:
        raise DecompositionError(
            f"subquestion {node['id']!r} produced no typed AST candidate"
        )
    options = leaf_candidates(selection)
    first_rejection = None
    first_guard_failure = None
    first_lowering_failure = None
    found = False
    for index, option in options:
        ok, reason = planner.guard(option.sql)
        if not ok:
            if first_guard_failure is None:
                first_guard_failure = reason
            continue
        rejection = ranked_leaf_grain_rejection(
            node["id"], option.query, feeds_cross
        ) or unstated_cutoff_rejection(
            node["id"], question, option.query.limit
        )
        if rejection is not None:
            if first_rejection is None:
                first_rejection = rejection
            continue
        try:
            child = shown_plan(selection.plan(index, node["id"], whole_tables=True))
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
                    # every input a view reads: a single view's source, a merge's two (a leaf read as a
                    # calculation lowers to one, engine/deterministic/lower.py lower_calculation)
                    **{field: names[getattr(view, field)]
                       for field in ("source", "left", "right") if hasattr(view, field)},
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
        # A sort that drops a value it ranks by (``shown``: a listing in the order its values first appear ranks by
        # the line each first appears on) passes on its order over the values it keeps; its rows keep the rest.
        return tuple(item for item in view.order
                     if not view.shown or not isinstance(item.value, ViewValue) or item.value.name in view.shown)
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


def shown_output(plan, columns, rows, catalogs) -> tuple[list[str], list[tuple]]:
    """The fused answer's columns and rows as the sheet shows them. An output column whose lineage is a column of the
    own-data path's tables (engine/program/store.py) is named by that column's header, as a single answer names it
    (engine/program/output.py answer_columns), unless another output column already has that name; and a key held as
    its fold (store.fold_column) shows each value as the column first spells it (engine/program/execute.py
    _displayed). Other columns (a figure a leaf computed), and plans over the request's own tables, are unchanged."""
    from engine.program.language import Col
    from engine.program.output import header
    from engine.program.store import day_column, fold_column, number_column, table_id_of, text_column

    names, rows = [str(name) for name in columns], [tuple(row) for row in rows]
    if not catalogs:
        return names, rows
    lineage = _merge_key_columns(plan.views, _shapes(plan.views, plan.tables, plan.slug), str(plan.output))
    words = {}
    for at, name in enumerate(columns):
        origin = lineage.get(name)
        catalog = next((held for held in catalogs if origin and table_id_of(origin[0]) in held.by_id), None)
        if catalog is None:
            continue
        table_id = table_id_of(origin[0])
        table = catalog.table(table_id)
        position = next((p for p in range(len(table.columns))
                         if origin[1] in (text_column(p), fold_column(p), number_column(p), day_column(p))), None)
        if position is None:
            continue
        shown = header(catalog, Col(table_id, position))
        if shown not in names:
            names[at] = shown
        if origin[1] == fold_column(position):
            words[at] = table.written_spelling[position]
    return names, [tuple(words[at].get(cell, cell) if at in words else cell for at, cell in enumerate(row)) for row in rows]


def _canonical(keys: dict[str, tuple[str, str]], lineage=None) -> dict[str, tuple[str, str]]:
    """Each output's lineage as the column whose values it holds (``lineage``: store columns the request's links
    connect, engine/program/store.py column_lineage); a column that holds no other's is its own."""
    return {name: (lineage or {}).get(origin, origin) for name, origin in keys.items()}


def duplicated_output_dimension(views, shapes, output: str, lineage=None) -> str | None:
    """Reject an answer whose grain repeats one physical dimension.

    Crossing two leaves that describe the SAME entity produces an output with two
    columns of identical lineage: every row then pairs a value with itself, and the
    table reads as a confident answer while being a modelling error. The closed
    grammar crosses DIFFERENT entities (customers x products), so repeated lineage
    is never a supported grain. Aggregate aliases carry no lineage and are exempt,
    so two measures that share a name cannot trip this.
    """
    seen: dict[tuple[str, str], str] = {}
    for name, origin in _canonical(_merge_key_columns(views, shapes, output), lineage).items():
        if origin in seen:
            return (
                f"the combined answer would repeat the {origin[0]}.{origin[1]} column "
                f"as both {seen[origin]!r} and {name!r}; cross two subquestions about "
                "DIFFERENT entities, or use one subquestion when only one is needed"
            )
        seen[origin] = name
    return None


def _bind_merge_keys(views, shapes, left, right, foreign_keys=(), lineage=None):
    """Bind aliases only when physical lineage matches or a direct FK proves identity.

    Ambiguous duplicate projections and unrelated columns sharing an alias fail
    closed. A direct, single-column inclusion dependency is sufficient to compare
    the same named entity key across its source and referenced table; composite or
    partial dependencies are deliberately not treated as identity. On the own-data
    path's store tables, ``lineage`` names those dependencies: a key the request's
    links find and each column a key reaches holds the values of the column at the
    end of its chain (``_canonical``).
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

    left_keys = _canonical(_merge_key_columns(views, shapes, left), lineage)
    right_keys = _canonical(_merge_key_columns(views, shapes, right), lineage)
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
