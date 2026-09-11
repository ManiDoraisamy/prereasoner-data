"""Adversarial contracts at the production decomposition owner and serving boundary."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock, patch

from engine.decomposition import (
    DecompositionError,
    _bind_merge_keys,
    build_decomposed_plan,
    compound_decomposition_required,
    leaf_measure_rejection,
    ranked_leaf_grain_rejection,
    validate_decomposition,
)
from engine.deterministic import (
    AggregateValue,
    ColumnValue,
    CombinedView,
    ProjectedView,
    ReducedView,
    SelectedValue,
    ViewValue,
)
from engine.deterministic.context import analysis_execution_context
from engine.knowledge_compose import ComposedKnowledgeQuery
from engine.sql_ast import Aggregate, ColumnRef, SelectItem, SelectQuery, SQLType, Star
from engine.sql_candidate import ScoredQuery


def _proposal():
    return {
        "subquestions": [
            {"id": "available", "question": "available products"},
            {"id": "purchased", "question": "purchased products"},
        ],
        "merges": [
            {"id": "missing", "op": "anti_join", "inputs": ["available", "purchased"]}
        ],
        "output": "missing",
        "grain": "one product",
    }


def _reject(callback):
    try:
        callback()
    except DecompositionError:
        return
    raise AssertionError("unsafe proposal was accepted")


def test_validation_is_closed_and_idempotent_at_transport_boundaries():
    normalized = validate_decomposition(_proposal())
    assert validate_decomposition(normalized) == normalized
    for op in ([], {}, None, 1):
        proposal = _proposal()
        proposal["merges"][0]["op"] = op
        _reject(lambda proposal=proposal: validate_decomposition(proposal))
    for node_id in ("Class", "café", "class", "a" * 41):
        proposal = _proposal()
        proposal["subquestions"][0]["id"] = node_id
        _reject(lambda proposal=proposal: validate_decomposition(proposal))
    proposal = _proposal()
    proposal["grain"] = float("nan")
    _reject(lambda: validate_decomposition(proposal))


def test_size_limit_counts_utf8_bytes_not_characters():
    proposal = {
        "subquestions": [
            {"id": "q" + str(i), "question": "😀" * 600, "label": "😀" * 80}
            for i in range(4)
        ],
        "merges": [
            {"id": "m0", "op": "anti_join", "inputs": ["q0", "q1"], "label": "😀" * 80},
            {"id": "m1", "op": "anti_join", "inputs": ["q2", "q3"], "label": "😀" * 80},
            {"id": "m2", "op": "anti_join", "inputs": ["m0", "m1"], "label": "😀" * 80},
        ],
        "output": "m2",
        "grain": "😀" * 120,
    }
    _reject(lambda: validate_decomposition(proposal))


def test_merge_keys_follow_dimensions_through_projection_not_aliases_or_measures():
    views = [
        CombinedView("a_combined", ("products",)),
        ReducedView(
            "a_total",
            "a_combined",
            (AggregateValue("total", "COUNT"),),
            (SelectedValue("product", ColumnValue("products", "id")),),
        ),
        ProjectedView(
            "a_result",
            "a_total",
            (
                SelectedValue("label", ViewValue("product")),
                SelectedValue("total", ViewValue("total")),
            ),
        ),
        CombinedView("b_combined", ("products",)),
        ProjectedView(
            "b_result",
            "b_combined",
            (
                SelectedValue("different_alias", ColumnValue("products", "id")),
                SelectedValue("total", ColumnValue("products", "price")),
            ),
        ),
    ]
    keys = _bind_merge_keys(views, {}, "a_result", "b_result")
    assert [(k.left, k.right) for k in keys] == [("label", "different_alias")]
    # Identical display names from unrelated tables are NOT an identity proof.
    views[-1] = replace(
        views[-1], values=(SelectedValue("label", ColumnValue("customers", "id")),)
    )
    _reject(lambda: _bind_merge_keys(views, {}, "a_result", "b_result"))
    # Nor can one dimension be silently bound to two duplicate projections.
    views[-1] = replace(
        views[-1],
        values=(
            SelectedValue("id1", ColumnValue("products", "id")),
            SelectedValue("id2", ColumnValue("products", "id")),
        ),
    )
    _reject(lambda: _bind_merge_keys(views, {}, "a_result", "b_result"))


def test_long_leaf_names_are_unique_postgres_identifiers_with_the_root_slug():
    tables = [{"name": "products", "columns": ["id"], "rows": [[1], [2]]}]
    schema = [
        {"table": "products", "name": "id", "affinity": "INTEGER", "values": [1, 2]}
    ]
    query = SelectQuery(
        (SelectItem(ColumnRef("products", "id", SQLType.INTEGER), "id"),), "products"
    )
    planner = Mock()
    planner.postgres_row_identity = False
    planner.search_ast.return_value = [
        ScoredQuery(query, "SELECT id FROM products", 1, ())
    ]
    planner.guard.return_value = (True, None)
    proposal = _proposal()
    ids = ["branch_" + "a" * 33, "branch_" + "a" * 32 + "b"]
    for node, node_id in zip(proposal["subquestions"], ids, strict=True):
        node["id"] = node_id
    proposal["merges"][0]["inputs"] = ids
    slug = "analysis_" + "s" * 31
    plan = build_decomposed_plan(planner, slug, tables, schema, (), proposal)
    names = [view.name for view in plan.views]
    assert all(
        name.startswith(slug + "_") and len(name.encode("utf-8")) <= 63
        for name in names
    )
    assert len(set(names)) == len(names)


def test_decomposition_expands_a_wildcard_leaf_before_dual_lowering():
    """A broad natural-language leaf still gets explicit names for both emitters."""
    tables = [{"name": "products", "columns": ["id"], "rows": [[1], [2]]}]
    schema = [
        {"table": "products", "name": "id", "affinity": "INTEGER", "values": [1, 2]}
    ]
    query = SelectQuery((SelectItem(Star()),), "products")
    planner = Mock()
    planner.postgres_row_identity = False
    planner.search_ast.return_value = [
        ScoredQuery(query, "SELECT * FROM products", 1, ())
    ]
    planner.guard.return_value = (True, None)
    plan = build_decomposed_plan(planner, "wildcard", tables, schema, (), _proposal())
    assert [view.name for view in plan.views].count("wildcard_missing") == 1
    assert any(
        getattr(view, "values", ())
        and view.values[0].value == ColumnValue("products", "id")
        for view in plan.views
    )


def test_measure_leaf_without_aggregation_is_rejected_not_answered():
    """A ranking-measure leaf that lost its SUM must clarify, never answer."""
    quantity = ColumnRef("purchase_items", "quantity", SQLType.INTEGER)
    name = ColumnRef("products", "product_name", SQLType.TEXT)
    raw = SelectQuery((SelectItem(name), SelectItem(quantity)), "purchase_items")
    summed = SelectQuery((SelectItem(name), SelectItem(Aggregate("SUM", quantity))),
                         "purchase_items", group_by=(name,))
    pool = [ScoredQuery(raw, "raw", 2, ()), ScoredQuery(summed, "summed", 1, ())]

    rejection = leaf_measure_rejection(
        "top_products", "top 3 products by units sold", raw, pool
    )
    assert rejection is not None and "restate" in rejection
    # The selected plan aggregating the measure satisfies the contract.
    assert leaf_measure_rejection(
        "top_products", "top 3 products by units sold", summed, pool
    ) is None
    # A rate phrase is not a summed measure; raw ordering stands.
    assert leaf_measure_rejection(
        "top_products", "top 3 products by unit price", raw, pool
    ) is None
    # Without an aggregated reading in the pool the raw plan is the planner's
    # honest best (for example a literal per-product units column): no veto.
    assert leaf_measure_rejection(
        "top_products", "top 3 products by units sold", raw, pool[:1]
    ) is None


def test_ranked_cross_input_must_stay_at_the_ranked_entity_grain():
    """A category ranking that also groups products is the wrong ranking grain."""
    from engine.sql_ast import OrderTerm

    category = ColumnRef("products", "category", SQLType.TEXT)
    product = ColumnRef("products", "product_name", SQLType.TEXT)
    revenue = Aggregate("SUM", ColumnRef("purchases", "line_total", SQLType.REAL))
    wide = SelectQuery(
        (SelectItem(category), SelectItem(product), SelectItem(revenue)),
        "purchases", group_by=(category, product),
        order_by=(OrderTerm(revenue, "DESC"),), limit=3,
    )
    narrow = SelectQuery(
        (SelectItem(category), SelectItem(revenue)),
        "purchases", group_by=(category,),
        order_by=(OrderTerm(revenue, "DESC"),), limit=3,
    )
    rejection = ranked_leaf_grain_rejection("top_categories", wide, True)
    assert rejection is not None and "ranked entity" in rejection
    # The single-dimension ranking, a non-cross consumer, and an unranked wide
    # evidence leaf all keep their current readings.
    assert ranked_leaf_grain_rejection("top_categories", narrow, True) is None
    assert ranked_leaf_grain_rejection("top_categories", wide, False) is None
    assert ranked_leaf_grain_rejection(
        "evidence", SelectQuery((SelectItem(category), SelectItem(product)), "purchases"), True
    ) is None


def test_failed_compound_probe_cannot_authorize_a_partial_composed_answer():
    planner = Mock()
    planner.ingest.side_effect = RuntimeError("planner unavailable")
    _reject(lambda: compound_decomposition_required(planner, [], "question"))
    host = ComposedKnowledgeQuery.__new__(ComposedKnowledgeQuery)
    host.qw = planner
    host._composed = Mock(return_value=True)
    host._run_engine = Mock(side_effect=AssertionError("partial answer executed"))
    with (
        analysis_execution_context(None, "c_" + "7" * 32),
        patch(
            "engine.decomposition.compound_decomposition_required",
            side_effect=DecompositionError("internal planner failure"),
        ),
    ):
        response = host._serve_locked([], "question", "c_" + "7" * 32)
    assert response["clarify"] and not response.get("result")
    assert "internal" not in response["reason"]
    host._run_engine.assert_not_called()
    planner.serve.assert_not_called()


TESTS = [
    test_validation_is_closed_and_idempotent_at_transport_boundaries,
    test_size_limit_counts_utf8_bytes_not_characters,
    test_merge_keys_follow_dimensions_through_projection_not_aliases_or_measures,
    test_long_leaf_names_are_unique_postgres_identifiers_with_the_root_slug,
    test_decomposition_expands_a_wildcard_leaf_before_dual_lowering,
    test_measure_leaf_without_aggregation_is_rejected_not_answered,
    test_ranked_cross_input_must_stay_at_the_ranked_entity_grain,
    test_failed_compound_probe_cannot_authorize_a_partial_composed_answer,
]


def main():
    failed = 0
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001 - report every regression before exiting
            failed += 1
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"decomposition: {len(TESTS) - failed} passed, {failed} failed")
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
