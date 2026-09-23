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
from engine.sql_ast import Aggregate, ColumnRef, SelectItem, SelectQuery, SetQuery, SQLType, Star
from engine.sql_candidate import ScoredQuery
from engine.sql_rank import PoolSelection


def _served(candidate):
    """What select_query returns when `candidate` is the only, executable pool member."""
    return PoolSelection((candidate,), frozenset(), (True,), ((-1.0, 1),), (0.0,), (0,), 0, 1)


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


def test_anti_join_evidence_must_preserve_the_complete_left_grain():
    """Backend parity must not certify evidence that dropped half a candidate pair."""
    from engine.deterministic.plan import CrossView

    views = [
        CombinedView("source", ("customers", "products")),
        ProjectedView("customers", "source", (SelectedValue(
            "customer_name", ColumnValue("customers", "customer_name")),)),
        ProjectedView("products", "source", (SelectedValue(
            "product_name", ColumnValue("products", "product_name")),)),
        CrossView("candidate_pairs", "customers", "products", "product_"),
        ProjectedView("product_evidence", "source", (SelectedValue(
            "product_name", ColumnValue("products", "product_name")),)),
    ]
    shapes = {
        "source": (), "customers": ("customer_name",),
        "products": ("product_name",),
        "candidate_pairs": ("customer_name", "product_name"),
        "product_evidence": ("product_name",),
    }
    _reject(lambda: _bind_merge_keys(
        views, shapes, "candidate_pairs", "product_evidence"
    ))

    views[-1] = ProjectedView("purchase_evidence", "source", (
        SelectedValue("customer_name", ColumnValue("customers", "customer_name")),
        SelectedValue("product_name", ColumnValue("products", "product_name")),
    ))
    shapes["purchase_evidence"] = ("customer_name", "product_name")
    keys = _bind_merge_keys(views, shapes, "candidate_pairs", "purchase_evidence")
    assert [(key.left, key.right) for key in keys] == [
        ("customer_name", "customer_name"), ("product_name", "product_name")
    ]


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
    planner.select_query.return_value = _served(
        ScoredQuery(query, "SELECT id FROM products", 1, ())
    )
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
    planner.select_query.return_value = _served(
        ScoredQuery(query, "SELECT * FROM products", 1, ())
    )
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


def test_an_answer_grain_cannot_repeat_one_physical_dimension():
    """A cross of two same-entity leaves pairs values with themselves."""
    from engine.decomposition import duplicated_output_dimension
    from engine.deterministic.plan import CrossView

    source = CombinedView("source", ("products", "customers"))
    # Two projections of the SAME physical column, crossed.
    left = ProjectedView("left", "source", (SelectedValue(
        "product_name", ColumnValue("products", "product_name")),))
    right = ProjectedView("right", "source", (SelectedValue(
        "product_name", ColumnValue("products", "product_name")),))
    crossed = CrossView("paired", "left", "right", "other_")
    shapes = {"source": (), "left": ("product_name",), "right": ("product_name",),
              "paired": ("product_name", "other_product_name")}
    assert duplicated_output_dimension((source, left, right, crossed),
                                       shapes, "paired") is not None
    # Distinct entities are the supported shape and must pass.
    right_customer = ProjectedView("right", "source", (SelectedValue(
        "customer_name", ColumnValue("customers", "customer_name")),))
    shapes_ok = {"source": (), "left": ("product_name",), "right": ("customer_name",),
                 "paired": ("product_name", "customer_name")}
    assert duplicated_output_dimension((source, left, right_customer, crossed),
                                       shapes_ok, "paired") is None
    # A single leaf keeps its own grain.
    assert duplicated_output_dimension(
        (source, left), {"source": (), "left": ("product_name",)}, "left") is None


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


def test_a_leaf_serves_the_chosen_ranking_with_its_measure_projected():
    """A decomposition leaf reads single queries in the arbiter's order, its choice first, each
    names-only ranking with its ORDER BY measure projected (the same rows in the same order),
    and serves the first reading that sums and ranks by the measure the question names. The
    Chrome release gate found both failure directions: a lower-ranked member that projected a
    SUM but ordered category-product pairs by product name, and a choice that ranked customers
    by units where the question said spend."""
    from engine.decomposition import leaf_candidate, leaf_measure_rejection
    from engine.sql_ast import Join, OrderTerm

    product = ColumnRef("products", "product_name", SQLType.TEXT)
    category = ColumnRef("products", "category", SQLType.TEXT)
    customer = ColumnRef("customers", "customer_name", SQLType.TEXT)
    quantity = Aggregate("SUM", ColumnRef("order_items", "quantity", SQLType.INTEGER))
    line_total = Aggregate("SUM", ColumnRef("order_items", "line_total", SQLType.REAL))
    join = Join("products", ColumnRef("order_items", "product_id", SQLType.INTEGER),
                ColumnRef("products", "product_id", SQLType.INTEGER))
    buyer = Join("customers", ColumnRef("order_items", "customer_id", SQLType.INTEGER),
                 ColumnRef("customers", "customer_id", SQLType.INTEGER))

    def ranking(entity, measure, *, show=False, limit=3):
        items = (SelectItem(entity),) + ((SelectItem(measure),) if show else ())
        joined = (buyer,) if entity.table == "customers" else (join,)
        return SelectQuery(items, "order_items", joins=joined, group_by=(entity,),
                           order_by=(OrderTerm(measure, "DESC"),), limit=limit)

    def pool_selection(*queries):
        pool = tuple(ScoredQuery(query, f"q{index}", 1.0, ()) for index, query in enumerate(queries))
        n = len(pool)
        return PoolSelection(pool, frozenset(), (True,) * n, ((-1.0, 9),) * n,
                             tuple(float(n - index) for index in range(n)), tuple(range(n)), 0, n)

    # The choice names the right measure without showing it: served projected, never the
    # lower-ranked reading that shows a SUM but orders category-product pairs by name.
    units = "top 3 product names by total quantity sold"
    names_only = ranking(product, quantity)
    other_reading = SelectQuery(
        (SelectItem(category), SelectItem(product), SelectItem(quantity)), "order_items",
        joins=(join,), group_by=(category, product), order_by=(OrderTerm(product, "DESC"),), limit=3)
    selection = pool_selection(names_only, other_reading)
    served = leaf_candidate(selection, "top_products", units, True)
    assert served.query == ranking(product, quantity, show=True)
    assert "leaf:ranked-measure-projected" in served.evidence
    assert leaf_measure_rejection("top_products", units, names_only, selection.pool) is not None
    assert "ranks by something other" in leaf_measure_rejection(
        "top_products", units, other_reading, selection.pool)

    # The choice ranks customers by units where the question says spend: the first reading
    # that sums and ranks by spend is served instead.
    spend = "top 2 customer names by total spend"
    by_units = ranking(customer, quantity, limit=2)
    by_spend = ranking(customer, line_total, show=True, limit=2)
    served = leaf_candidate(pool_selection(by_units, by_spend), "top_customers", spend, True)
    assert served.query == by_spend

    # A choice that already shows the named measure is served as it is.
    shown = pool_selection(by_spend)
    assert leaf_candidate(shown, "top_customers", spend, True) is shown.pool[0]

    # A compound choice yields to the best single query; with none, there is no leaf query.
    compound = SetQuery(names_only, "EXCEPT", names_only)
    assert leaf_candidate(pool_selection(compound, names_only), "top_products", units, True).query \
        == ranking(product, quantity, show=True)
    assert leaf_candidate(pool_selection(compound), "top_products", units, True) is None


TESTS = [
    test_validation_is_closed_and_idempotent_at_transport_boundaries,
    test_size_limit_counts_utf8_bytes_not_characters,
    test_merge_keys_follow_dimensions_through_projection_not_aliases_or_measures,
    test_anti_join_evidence_must_preserve_the_complete_left_grain,
    test_long_leaf_names_are_unique_postgres_identifiers_with_the_root_slug,
    test_decomposition_expands_a_wildcard_leaf_before_dual_lowering,
    test_a_leaf_serves_the_chosen_ranking_with_its_measure_projected,
    test_measure_leaf_without_aggregation_is_rejected_not_answered,
    test_ranked_cross_input_must_stay_at_the_ranked_entity_grain,
    test_an_answer_grain_cannot_repeat_one_physical_dimension,
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
