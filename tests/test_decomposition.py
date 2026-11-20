"""Adversarial contracts at the production decomposition owner and serving boundary."""

from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock, patch

from engine.decomposition import (
    DecompositionError,
    _bind_merge_keys,
    build_decomposed_plan,
    ranked_leaf_grain_rejection,
    search_probe,
    unstated_cutoff_rejection,
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
from engine.sql_ast import (
    Aggregate,
    ColumnRef,
    SelectItem,
    SelectQuery,
    SQLType,
)
from engine.sql_candidate import ScoredQuery
from engine.sql_rank import PoolSelection


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


def test_a_node_id_is_a_readable_name_because_it_names_the_sheets():
    """Chrome gate, 2026-10-02 (complex-category-gaps, "only use the top 2 customers"): the proposal
    named its leaves "c", "p" and "ev", and the user read the sheets "c combined", "c top results"
    and "ev result" and the result column "p_sum". A node id names the sheets, so a letter or two is
    rejected with the reason, which the model corrects within its proposal budget."""
    proposal = {
        "subquestions": [
            {"id": "c", "question": "top 2 customer names by total spend"},
            {"id": "p", "question": "top 3 category names by total revenue"},
            {"id": "ev", "question": "customer name and category name for each purchase"},
        ],
        "merges": [
            {"id": "pairs", "op": "cross", "inputs": ["c", "p"]},
            {"id": "never_bought", "op": "anti_join", "inputs": ["pairs", "ev"]},
        ],
        "output": "never_bought",
        "grain": "one customer-category pair",
    }
    try:
        validate_decomposition(proposal)
    except DecompositionError as exc:
        assert "'c' names the sheets the user reads" in str(exc), exc
        assert "readable snake_case name" in str(exc), exc
    else:
        raise AssertionError("a one-letter node id was accepted")
    renamed = {
        **proposal,
        "subquestions": [
            {"id": "top_customers", "question": "top 2 customer names by total spend"},
            {"id": "top_categories", "question": "top 3 category names by total revenue"},
            {"id": "purchases", "question": "customer name and category name for each purchase"},
        ],
        "merges": [
            {"id": "pairs", "op": "cross", "inputs": ["top_customers", "top_categories"]},
            {"id": "never_bought", "op": "anti_join", "inputs": ["pairs", "purchases"]},
        ],
    }
    assert validate_decomposition(renamed)["subquestions"][0]["label"] == "top customers"
    for short in ("m1", "q0"):
        merge = {**renamed, "merges": [{**renamed["merges"][0], "id": short},
                                       {**renamed["merges"][1], "inputs": [short, "purchases"]}]}
        _reject(lambda merge=merge: validate_decomposition(merge))


def test_size_limit_counts_utf8_bytes_not_characters():
    proposal = {
        "subquestions": [
            {"id": "leaf_" + str(i), "question": "😀" * 600, "label": "😀" * 80}
            for i in range(4)
        ],
        "merges": [
            {"id": "merge_0", "op": "anti_join", "inputs": ["leaf_0", "leaf_1"], "label": "😀" * 80},
            {"id": "merge_1", "op": "anti_join", "inputs": ["leaf_2", "leaf_3"], "label": "😀" * 80},
            {"id": "merge_2", "op": "anti_join", "inputs": ["merge_0", "merge_1"], "label": "😀" * 80},
        ],
        "output": "merge_2",
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


def test_merge_key_alias_can_follow_only_a_complete_direct_foreign_key():
    views = [
        CombinedView("source", ("products", "purchases")),
        ProjectedView("products", "source", (SelectedValue(
            "product_name", ColumnValue("products", "product_name")),)),
        ProjectedView("purchases", "source", (SelectedValue(
            "product_name", ColumnValue("purchases", "product_name")),)),
    ]
    fks = [{"from_table": "purchases", "from_col": "product_name",
            "to_table": "products", "to_col": "product_name", "inclusion": 1.0}]
    keys = _bind_merge_keys(views, {}, "products", "purchases", fks)
    assert [(key.left, key.right) for key in keys] == [("product_name", "product_name")]
    fks[0]["inclusion"] = 0.99
    _reject(lambda: _bind_merge_keys(views, {}, "products", "purchases", fks))


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


def test_store_columns_a_key_connects_bind_by_the_column_they_hold():
    """On the own-data path's store tables a key-reached column holds the values of the column it was read from
    (engine/program/store.py column_lineage): "all product names" on the products tab and "product names bought in
    Paris" on the purchases tab bind one anti-join key (complex-unsold-products, demo gate 2026-11-20), and a cross of
    a column with one that holds its values repeats one dimension. Without the lineage the two stay different columns."""
    from engine.decomposition import duplicated_output_dimension
    from engine.deterministic.plan import CrossView

    views = [
        CombinedView("source", ("_t0", "_t1")),
        ProjectedView("products", "source", (SelectedValue("value", ColumnValue("_t0", "c1_f")),)),
        ProjectedView("purchases", "source", (SelectedValue("value", ColumnValue("_t1", "c3_f")),)),
    ]
    lineage = {("_t1", "c3_f"): ("_t0", "c1_f")}
    keys = _bind_merge_keys(views, {}, "products", "purchases", (), lineage)
    assert [(key.left, key.right) for key in keys] == [("value", "value")]
    _reject(lambda: _bind_merge_keys(views, {}, "products", "purchases", ()))
    _reject(lambda: _bind_merge_keys(views, {}, "products", "purchases", (), {("_t1", "c2_f"): ("_t0", "c1_f")}))
    crossed = CrossView("paired", "products", "purchases", "other_")
    shapes = {"source": (), "products": ("value",), "purchases": ("value",), "paired": ("value", "other_value")}
    assert duplicated_output_dimension((*views, crossed), shapes, "paired") is None
    assert duplicated_output_dimension((*views, crossed), shapes, "paired", lineage) is not None


def test_a_fused_answer_shows_the_sheets_spelling_of_a_folded_key():
    """The own-data path keys a listing by a column's fold; a fused answer whose output column is such a fold shows
    each value as the sheet first spells it ("delta" was served for "Delta", demo gate 2026-11-20), and each column
    drawn from a sheet column is named by its header ("value" was served for "product_name", demo gate 2026-11-20).
    A plan over the request's own tables is unchanged."""
    from engine.decomposition import shown_output
    from engine.deterministic.plan import AnalysisPlan

    class Table:
        columns = ("id", "name")
        written_spelling = {1: {"delta": "Delta", "omega": "Omega"}}

        def column_name(self, position):
            return self.columns[position]

    class Workbook:
        by_id = {"t0": Table()}

        def table(self, table_id):
            return self.by_id[table_id]

    plan = Mock(spec=AnalysisPlan)
    plan.views = (CombinedView("source", ("_t0",)),
                  ProjectedView("names", "source", (SelectedValue("value", ColumnValue("_t0", "c1_f")),
                                                    SelectedValue("n", ColumnValue("_t0", "c0_n")))))
    plan.tables, plan.slug, plan.output = (), "s", "names"
    with patch("engine.decomposition._shapes", lambda views, tables, slug: {"source": (), "names": ("value", "n")}):
        assert shown_output(plan, ["value", "n"], [("delta", 1), ("omega", 2), ("zeta", 3)], [Workbook()]) == (
            ["name", "id"], [("Delta", 1), ("Omega", 2), ("zeta", 3)])
        assert shown_output(plan, ["value", "n"], [], [Workbook()]) == (["name", "id"], []), "an empty answer is named too"
        # contrastive: a header another output column already has keeps the plan's name
        assert shown_output(plan, ["value", "name"], [("delta", 1)], [Workbook()])[0] == ["value", "name"]
        # negative: a plan over the request's own tables is unchanged
        assert shown_output(plan, ["value", "n"], [("delta", 1)], []) == (["value", "n"], [("delta", 1)])


def test_failed_compound_probe_cannot_authorize_a_partial_composed_answer():
    planner = Mock()
    planner.ingest.side_effect = RuntimeError("planner unavailable")
    _reject(lambda: search_probe(planner, [], "question"))
    host = ComposedKnowledgeQuery.__new__(ComposedKnowledgeQuery)
    host.qw = planner
    host._composed = Mock(return_value=True)
    host._run_engine = Mock(side_effect=AssertionError("partial answer executed"))
    with (
        analysis_execution_context(None, "c_" + "7" * 32),
        patch(
            "engine.decomposition.search_probe",
            side_effect=DecompositionError("internal planner failure"),
        ),
    ):
        response = host.serve([], "question", "c_" + "7" * 32)
    assert response["clarify"] and not response.get("result")
    assert "internal" not in response["reason"]
    host._run_engine.assert_not_called()
    planner.serve.assert_not_called()


def _catalog(rows, name="products"):
    """One workbook tab, compiled as the own-data path compiles a request's grid."""
    from engine.program.catalog import Catalog
    from engine.sheet import compile_workbook, workbook_from_json

    return Catalog.from_model(compile_workbook(workbook_from_json({"name": "", "tabs": [{"name": name, "rows": rows}]})))


PRODUCTS = [["id", "name"], [1, "Coat"], [2, "Hat"]]


def _programs(catalog, *programs, selected=0):
    """What select_query returns for a leaf: the Decisions path's listed programs over ``catalog``, ranked in their
    order, on the leaf's tables (engine/program/store.py)."""
    from engine.program.lowering import to_query
    from engine.program.store import RequestTables
    from engine.sql_ast import render_query

    pool = []
    for program in programs:
        query = to_query(program, catalog)
        pool.append(ScoredQuery(query, render_query(query), 1.0, ("a reading",)))
    return PoolSelection(tuple(pool), tuple(range(len(pool))), selected, programs=programs,
                         rows=tuple(((1,),) for _ in programs), catalog=catalog, tables=RequestTables(catalog))


def _names_listing(catalog):
    from engine.program.language import Col, Show
    return Show("t0", (Col("t0", 1),), listing=True)


def _leaf_planner(selections):
    planner = Mock()
    planner.select_query.side_effect = lambda leaf, _tables: selections(leaf)
    planner.guard.return_value = (True, None)
    return planner


def test_long_leaf_names_are_unique_postgres_identifiers_with_the_root_slug():
    catalog = _catalog(PRODUCTS)
    planner = _leaf_planner(lambda leaf: _programs(catalog, _names_listing(catalog)))
    proposal = _proposal()
    ids = ["branch_" + "a" * 33, "branch_" + "a" * 32 + "b"]
    for node, node_id in zip(proposal["subquestions"], ids, strict=True):
        node["id"] = node_id
    proposal["merges"][0]["inputs"] = ids
    slug = "analysis_" + "s" * 31
    plan = build_decomposed_plan(planner, slug, [], (), proposal, question="available products never purchased")
    names = [view.name for view in plan.views]
    assert all(name.startswith(slug + "_") and len(name.encode("utf-8")) <= 63 for name in names)
    assert len(set(names)) == len(names)


def test_long_leaf_names_keep_hash_free_labels():
    """2026-10-02 Chrome gate (complex-promotions, complex-unsold-products): under the analysis
    "top_customers_never_bought_top_products" a leaf view's name passed PostgreSQL's 63 bytes, was
    cut and given a hash, and its label was read back from the cut name: "top customers top r
    b966d67a". Each view keeps the name it had before the prefix, so the label reads as the step."""
    import re

    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from engine.deterministic.service import DeterministicAnalysis
    from engine.prepared import SharedConnection

    catalog = _catalog(PRODUCTS)
    selection = _programs(catalog, _names_listing(catalog))
    planner = _leaf_planner(lambda leaf: selection)
    proposal = _proposal()
    ids = ["top_customers", "top_products_bought"]
    for node, node_id in zip(proposal["subquestions"], ids, strict=True):
        node["id"] = node_id
    proposal["merges"][0]["inputs"] = ids
    proposal["merges"][0]["id"] = "customer_product_pairs_never_bought"
    proposal["output"] = "customer_product_pairs_never_bought"
    slug = "top_customers_never_bought_top_products"
    plan = build_decomposed_plan(planner, slug, [], (), proposal, question="top customers never bought top products")
    names = [view.name for view in plan.views]
    hashed = [name for name in names if re.search(r"_[0-9a-f]{8}$", name)]
    assert hashed and all(len(name.encode("utf-8")) <= 63 for name in names), names
    shared = SharedConnection(selection.tables.connection)
    engine = create_engine("sqlite+pysqlite://", creator=lambda: shared, poolclass=StaticPool)
    record = DeterministicAnalysis(plan, conversation_schema="main", dataset_version="test").run(
        engine, mode="verify", estimated_rows=2).record()
    views = {view["name"]: view for view in record["views"]}
    assert set(hashed) & set(views), (hashed, list(views))
    for view in record["views"]:
        assert not re.search(r"[0-9a-f]{8}$", view["logical_name"]), view["logical_name"]
        assert not re.search(r"[0-9a-f]{8}$", view["label"]), view["label"]
    assert views[plan.output]["label"] == "customer product pairs never bought", views[plan.output]
    assert {view["label"].split()[0] for view in record["views"]} >= {"top", "customer"}


def test_decomposition_tries_next_ranked_candidate_when_best_cannot_dual_lower():
    """A top reading the deterministic emitters cannot run must not hide a lower-ranked one that they can."""
    from engine.deterministic.lower import UnsupportedDeterministicPlan

    catalog = _catalog(PRODUCTS)
    listing = _names_listing(catalog)
    selection = _programs(catalog, listing, listing)
    lowered = []

    class FirstUnsupported(type(selection)):
        def plan(self, index, slug, whole_tables=False):
            lowered.append(index)
            if index == 0:
                raise UnsupportedDeterministicPlan("not in the dual subset")
            return super().plan(index, slug, whole_tables)

    from dataclasses import fields
    selection = FirstUnsupported(**{field.name: getattr(selection, field.name) for field in fields(selection)})
    planner = _leaf_planner(lambda leaf: selection)
    plan = build_decomposed_plan(planner, "fallback", [], (), _proposal(), question="available products never purchased")
    assert lowered == [0, 1, 0, 1] and plan.output, lowered


def test_a_leaf_keeps_only_a_cutoff_the_question_states():
    """The Chrome pass (2026-09-24) caught this. Told that cross inputs need explicit limits,
    the proposer answered "products no customer from Paris has bought" by crossing "the top 100
    customer names from Paris" with "the top 100 product names", and the engine served 14
    customer-product pairs. A leaf may keep only a row cutoff the decomposed question states."""
    from engine.program.language import Col, Rank
    from engine.program.names import COUNT, ENTITY_GROUP

    paris = "List the product names that no customer from Paris has bought, ordered by product name."
    rejection = unstated_cutoff_rejection("products", paris, 100)
    assert rejection is not None and "100" in rejection and "does not state" in rejection
    # A stated cutoff passes in digits or in words; any other cutoff does not.
    ranked = "Find the top three products by units sold and the top 2 customers by total spend."
    assert unstated_cutoff_rejection("top_products", ranked, 3) is None
    assert unstated_cutoff_rejection("top_customers", ranked, 2) is None
    assert unstated_cutoff_rejection("top_customers", ranked, 5) is not None
    # One row is a singular superlative's reading; an unlimited leaf has no cutoff at all.
    assert unstated_cutoff_rejection(
        "best", "customers who never bought the best-selling product", 1) is None
    assert unstated_cutoff_rejection("products", paris, None) is None

    # The compiler applies it to every leaf, whichever merge consumes it.
    catalog = _catalog(PRODUCTS)
    capped = Rank(ENTITY_GROUP, "t0", key=Col("t0", 1), function=COUNT, count=100)
    planner = _leaf_planner(lambda leaf: _programs(catalog, capped))
    _reject(lambda: build_decomposed_plan(planner, "capped", [], (), _proposal(), question=paris))
    plan = build_decomposed_plan(planner, "capped", [], (), _proposal(),
                                 question="the first 100 available products never purchased")
    assert plan.output


def test_anti_join_evidence_takes_the_first_reading_that_keeps_the_left_grain():
    """Production 2026-10-01, complex-promotions: the evidence leaf "For each customer, list
    every product name they have ever bought" was ranked first as a product-only reading, so
    the anti-join could not tell which customer bought what and the plan failed. The
    customer-and-product reading ranked next is the evidence the merge needs; a reading that
    keeps the left grain is served as it is, and with none the first reading's error stands.
    Every leaf is a program over the one tab (a merge binds keys by the column each reads)."""
    from engine.deterministic.plan import AntiJoinView
    from engine.program.language import Breakdown, Col, Rank, Show
    from engine.program.names import COUNT, ENTITY_GROUP, SUM

    catalog = _catalog([["customer", "product", "quantity", "line_total"], ["Mina", "Coat", 2, 20.0],
                        ["Ola", "Hat", 1, 5.0], ["Mina", "Hat", 3, 9.0]], "order_items")
    top_customers = Rank(ENTITY_GROUP, "t0", key=Col("t0", 0), measure=Col("t0", 3), function=SUM, count=2)
    top_products = Rank(ENTITY_GROUP, "t0", key=Col("t0", 1), measure=Col("t0", 2), function=SUM, count=3)
    products_only = Show("t0", (Col("t0", 1),), listing=True)
    purchases = Breakdown(COUNT, None, "t0", keys=(Col("t0", 0), Col("t0", 1)))
    leaves = {"top 2 customer names by total spend": (top_customers,),
              "top 3 product names by total quantity sold": (top_products,)}
    evidence = "For each customer, list every product name they have ever bought"
    proposal = {
        "subquestions": [
            {"id": "top_customers", "question": "top 2 customer names by total spend"},
            {"id": "top_products", "question": "top 3 product names by total quantity sold"},
            {"id": "purchases", "question": evidence},
        ],
        "merges": [
            {"id": "pairs", "op": "cross", "inputs": ["top_customers", "top_products"]},
            {"id": "missing", "op": "anti_join", "inputs": ["pairs", "purchases"]},
        ],
        "output": "missing", "grain": "one customer-product pair",
    }
    question = ("Find the top 3 products by units sold and the top 2 customers by total spend, "
                "then list each top-customer and top-product pair where that customer has never "
                "bought that product.")

    def plan_with(*readings):
        planner = _leaf_planner(lambda leaf: _programs(catalog, *leaves.get(leaf, readings)))
        return build_decomposed_plan(planner, "promotions", [], (), proposal, question=question)

    plan = plan_with(products_only, purchases)
    assert [section.id for section in plan.sections] == ["top_customers", "top_products", "purchases", "pairs", "missing"]
    missing = next(view for view in plan.views if isinstance(view, AntiJoinView))
    assert len(missing.keys) == 2, missing.keys
    assert plan_with(purchases, products_only).output
    try:
        plan_with(products_only)
    except DecompositionError as exc:
        assert "must also preserve the left-side dimension(s)" in str(exc), exc
    else:
        raise AssertionError("evidence without the left grain was accepted")


TESTS = [
    test_anti_join_evidence_takes_the_first_reading_that_keeps_the_left_grain,
    test_long_leaf_names_are_unique_postgres_identifiers_with_the_root_slug,
    test_long_leaf_names_keep_hash_free_labels,
    test_decomposition_tries_next_ranked_candidate_when_best_cannot_dual_lower,
    test_a_leaf_keeps_only_a_cutoff_the_question_states,
    test_validation_is_closed_and_idempotent_at_transport_boundaries,
    test_size_limit_counts_utf8_bytes_not_characters,
    test_a_node_id_is_a_readable_name_because_it_names_the_sheets,
    test_merge_keys_follow_dimensions_through_projection_not_aliases_or_measures,
    test_merge_key_alias_can_follow_only_a_complete_direct_foreign_key,
    test_anti_join_evidence_must_preserve_the_complete_left_grain,
    test_ranked_cross_input_must_stay_at_the_ranked_entity_grain,
    test_an_answer_grain_cannot_repeat_one_physical_dimension,
    test_store_columns_a_key_connects_bind_by_the_column_they_hold,
    test_a_fused_answer_shows_the_sheets_spelling_of_a_folded_key,
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
