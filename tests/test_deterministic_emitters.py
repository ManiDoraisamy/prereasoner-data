"""Hermetic contract tests for the paired deterministic SQL/Python emitters."""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text

from engine.deterministic import (
    AggregateValue,
    AnalysisPlan,
    BinaryValue,
    CalculatedView,
    ColumnSpec,
    ColumnValue,
    CombinedView,
    DeterministicAnalysis,
    EnrichedView,
    Enrichment,
    ExecutionMode,
    FilteredView,
    LiteralValue,
    PredicateValue,
    ReducedView,
    RelationshipSpec,
    SelectedValue,
    TableSpec,
    ViewValue,
    lower_select_query,
)
from engine.deterministic.context import (
    analysis_execution_context,
    current_execution_record,
    set_execution_record,
)
from engine.deterministic.emitter import PythonEmitter, SQLEmitter
from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
    ColumnRef,
    Comparison,
    Join,
    Literal,
    SelectItem,
    SelectQuery,
    SQLType,
)


def _plan() -> AnalysisPlan:
    customers = TableSpec(
        name="customers",
        class_name="Customer",
        attribute="customers",
        schema="conversation",
        columns=(
            ColumnSpec(
                "customer_id",
                "customer_id",
                SQLType.INTEGER,
                primary_key=True,
                nullable=False,
            ),
            ColumnSpec("name", "name", SQLType.TEXT, nullable=False),
        ),
    )
    country = TableSpec(
        name="country",
        class_name="Country",
        attribute="country",
        schema="knowledgebase",
        columns=(
            ColumnSpec("qid", "qid", SQLType.TEXT, primary_key=True, nullable=False),
            ColumnSpec("name", "name", SQLType.TEXT, nullable=False),
        ),
    )
    city = TableSpec(
        name="city",
        class_name="City",
        attribute="city",
        schema="knowledgebase",
        columns=(
            ColumnSpec("qid", "qid", SQLType.TEXT, primary_key=True, nullable=False),
            ColumnSpec("name", "name", SQLType.TEXT, nullable=False),
            ColumnSpec("country", "country", SQLType.TEXT, nullable=False),
        ),
        relationships=(RelationshipSpec("country", "country", ("country",), ("qid",)),),
    )
    orders = TableSpec(
        name="orders",
        class_name="Order",
        attribute="orders",
        schema="conversation",
        columns=(
            ColumnSpec(
                "order_id",
                "order_id",
                SQLType.INTEGER,
                primary_key=True,
                nullable=False,
            ),
            ColumnSpec("customer_id", "customer_id", SQLType.INTEGER, nullable=False),
            ColumnSpec("amount", "amount", SQLType.REAL, nullable=False),
            ColumnSpec("city", "city", SQLType.TEXT, nullable=False),
        ),
        relationships=(
            RelationshipSpec(
                "customer_id", "customers", ("customer_id",), ("customer_id",)
            ),
            RelationshipSpec("city", "city", ("city",), ("qid",)),
        ),
    )
    return AnalysisPlan(
        slug="total_amount",
        tables=(orders, customers, city, country),
        views=(
            CombinedView("total_amount_combined", ("orders", "customers")),
            EnrichedView(
                "total_amount_enriched",
                "total_amount_combined",
                (
                    Enrichment("city", "orders", ("city",)),
                    Enrichment("country", "orders", ("city", "country")),
                ),
            ),
            FilteredView(
                "total_amount_filtered",
                "total_amount_enriched",
                PredicateValue(
                    ColumnValue("country", "name"), "=", LiteralValue("France")
                ),
            ),
            CalculatedView(
                "total_amount_calculated",
                "total_amount_filtered",
                (
                    SelectedValue(
                        "gross_amount",
                        BinaryValue(
                            ColumnValue("orders", "amount"),
                            "*",
                            LiteralValue(Decimal(2)),
                        ),
                    ),
                ),
            ),
            ReducedView(
                "total_amount_total",
                "total_amount_calculated",
                (AggregateValue("total_amount", "SUM", ViewValue("gross_amount")),),
            ),
        ),
    )


def test_python_source_is_readable_object_graph_and_feed_forward_pipeline():
    generated = PythonEmitter().emit(
        _plan(), dataset_version="d1", knowledgebase_release="kb1"
    )
    assert generated.entrypoint == "orders_customers.py"
    assert set(generated.files) == {
        "base.py",
        "orders.py",
        "customers.py",
        "city.py",
        "country.py",
        "orders_customers.py",
    }
    orders = generated.files["orders.py"]
    assert "_customer_id_value: Mapped[int]" in orders
    assert "customer_id: Mapped['Customer'] = relationship(" in orders
    assert "city: Mapped['City'] = relationship(" in orders
    wrapper = generated.files["orders_customers.py"]
    assert "class OrdersCustomers:" in wrapper
    assert "def total_amount(self) -> AnalysisResult:" in wrapper
    assert (
        "from engine.deterministic.operators import EQ, MULTIPLY, SUM, View"
        in wrapper
    )
    assert "total_amount_enriched = total_amount_combined.for_each(" in wrapper
    assert "total_amount_filtered = total_amount_enriched.filter(" in wrapper
    assert "total_amount_calculated = total_amount_filtered.for_each(" in wrapper
    assert "total_amount_total = total_amount_calculated.reduce(" in wrapper
    assert "SUM(result.total_amount, row.gross_amount)" in wrapper


def test_both_emitters_are_byte_deterministic_and_have_matching_stages():
    plan = _plan()
    first_python = PythonEmitter().emit(plan)
    second_python = PythonEmitter().emit(plan)
    first_sql = SQLEmitter().emit(plan)
    second_sql = SQLEmitter().emit(plan)
    assert first_python.files == second_python.files
    assert first_python.source_sha256 == second_python.source_sha256
    assert first_sql.source == second_sql.source
    assert first_sql.source_sha256 == second_sql.source_sha256
    assert [statement.split('"', 2)[1] for statement in first_sql.statements] == [
        view.name for view in plan.views
    ]
    assert 'FROM "total_amount_combined"' in first_sql.statements[1]
    assert 'FROM "total_amount_enriched"' in first_sql.statements[2]
    assert 'FROM "total_amount_filtered"' in first_sql.statements[3]
    assert 'FROM "total_amount_calculated"' in first_sql.statements[4]


def test_generated_python_and_sql_execute_to_the_same_result():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS conversation"))
        connection.execute(text("ATTACH DATABASE ':memory:' AS knowledgebase"))
        connection.execute(
            text(
                "CREATE TABLE conversation.customers (customer_id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE conversation.orders ("
                "order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, amount NUMERIC NOT NULL, city TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledgebase.country (qid TEXT PRIMARY KEY, name TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE knowledgebase.city (qid TEXT PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO conversation.customers VALUES (1, 'Ada'), (2, 'Lin')")
        )
        connection.execute(
            text(
                "INSERT INTO knowledgebase.country VALUES ('Q142', 'France'), ('Q30', 'United States')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO knowledgebase.city VALUES "
                "('Q90', 'Paris', 'Q142'), ('Q60', 'New York City', 'Q30')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO conversation.orders VALUES "
                "(10, 1, 12.50, 'Q90'), (11, 1, 7.50, 'Q90'), (12, 2, 100, 'Q60')"
            )
        )
        analysis = DeterministicAnalysis(_plan(), conversation_schema="conversation")
        result = analysis.run(connection, mode=ExecutionMode.VERIFY, estimated_rows=3)
    assert result.rows == ({"total_amount": 40},)
    record = result.record()
    assert [view["op"] for view in record["views"]] == [
        "join",
        "world_join",
        "filter",
        "convert",
        "group_agg",
    ]
    assert [len(view["rows"]) for view in record["views"]] == [3, 3, 2, 2, 1]
    assert record["views"][-1]["columns"] == ["total_amount"]


def test_auto_policy_uses_python_only_below_the_configured_limit():
    from engine.deterministic.runtime import (
        choose_execution_mode,
        debug_generation_enabled,
    )

    assert (
        choose_execution_mode("auto", estimated_rows=10, python_row_limit=10)
        is ExecutionMode.PYTHON
    )
    assert (
        choose_execution_mode("auto", estimated_rows=11, python_row_limit=10)
        is ExecutionMode.SQL
    )
    assert choose_execution_mode("verify", estimated_rows=1) is ExecutionMode.VERIFY
    assert debug_generation_enabled("development")
    assert not debug_generation_enabled("production")
    assert debug_generation_enabled("production", explicit=True)


def test_development_debug_tree_contains_the_exact_executed_source():
    generated = PythonEmitter().emit(_plan())
    with tempfile.TemporaryDirectory() as directory:
        destination = generated.write_debug(directory, "c_" + "1" * 32, 3)
        assert (
            destination
            == Path(directory).resolve() / ("c_" + "1" * 32) / "total_amount"
        )
        assert (destination / generated.entrypoint).read_bytes() == generated.files[
            generated.entrypoint
        ].encode("utf-8")
        assert '"revision": 3' in (destination / "manifest.json").read_text(
            encoding="utf-8"
        )
        (destination / "stale.py").write_text("stale = True\n", encoding="utf-8")
        replaced = generated.write_debug(directory, "c_" + "1" * 32, 4)
        assert replaced == destination
        assert not (destination / "stale.py").exists()
        assert '"revision": 4' in (destination / "manifest.json").read_text(
            encoding="utf-8"
        )


def test_plan_rejects_a_stage_that_skips_its_predecessor():
    plan = _plan()
    views = list(plan.views)
    views[3] = CalculatedView(
        "total_amount_calculated",
        "total_amount_combined",
        (SelectedValue("gross_amount", ColumnValue("orders", "amount")),),
    )
    try:
        AnalysisPlan(plan.slug, plan.tables, tuple(views))
        raise AssertionError("a non-feed-forward view chain was accepted")
    except ValueError as exc:
        assert "immediately preceding" in str(exc)


def test_existing_typed_sql_ast_lowers_without_parsing_rendered_sql():
    amount = ColumnRef("orders", "amount", SQLType.REAL)
    query = SelectQuery(
        select=(
            SelectItem(
                Aggregate(
                    "SUM",
                    BinaryExpr(amount, "*", Literal(2.0, SQLType.REAL)),
                ),
                "total_amount",
            ),
        ),
        from_table="orders",
        joins=(
            Join(
                "customers",
                ColumnRef("orders", "customer_id", SQLType.INTEGER),
                ColumnRef("customers", "customer_id", SQLType.INTEGER),
            ),
        ),
        where=Comparison(amount, ">", Literal(Decimal(5), SQLType.REAL)),
    )
    schema = [
        {
            "table": "orders",
            "name": "order_id",
            "affinity": "INTEGER",
            "values": [10, 11],
        },
        {
            "table": "orders",
            "name": "customer_id",
            "affinity": "INTEGER",
            "values": [1, 1],
        },
        {
            "table": "orders",
            "name": "amount",
            "affinity": "REAL",
            "values": ["12.50", "7.50"],
        },
        {
            "table": "customers",
            "name": "customer_id",
            "affinity": "INTEGER",
            "values": [1],
        },
        {
            "table": "customers",
            "name": "name",
            "affinity": "TEXT",
            "values": ["Ada"],
        },
    ]
    foreign_keys = [
        {
            "from_table": "orders",
            "from_cols": ["customer_id"],
            "to_table": "customers",
            "to_cols": ["customer_id"],
        }
    ]
    plan = lower_select_query("total_amount", query, schema, foreign_keys)
    assert [type(view).__name__ for view in plan.views] == [
        "CombinedView",
        "FilteredView",
        "CalculatedView",
        "ReducedView",
    ]
    assert plan.table("orders").relationship("customer_id").target_table == "customers"
    wrapper = PythonEmitter().emit(plan).files["orders_customers.py"]
    assert "MULTIPLY(row.orders.amount, Decimal('2.0'))" in wrapper
    assert "total_amount_total = total_amount_calculated.reduce(" in wrapper


def test_lowering_uses_the_joined_relationship_when_two_edges_share_tables():
    query = SelectQuery(
        select=(
            SelectItem(ColumnRef("customers", "name", SQLType.TEXT), "name"),
        ),
        from_table="orders",
        joins=(
            Join(
                "customers",
                ColumnRef("orders", "billing_customer_id", SQLType.INTEGER),
                ColumnRef("customers", "customer_id", SQLType.INTEGER),
            ),
        ),
    )
    schema = [
        {"table": "orders", "name": "order_id", "affinity": "INTEGER"},
        {"table": "orders", "name": "owner_id", "affinity": "INTEGER"},
        {
            "table": "orders",
            "name": "billing_customer_id",
            "affinity": "INTEGER",
        },
        {"table": "customers", "name": "customer_id", "affinity": "INTEGER"},
        {"table": "customers", "name": "name", "affinity": "TEXT"},
    ]
    foreign_keys = [
        {
            "from_table": "orders",
            "from_col": "owner_id",
            "to_table": "customers",
            "to_col": "customer_id",
        },
        {
            "from_table": "orders",
            "from_col": "billing_customer_id",
            "to_table": "customers",
            "to_col": "customer_id",
        },
    ]
    plan = lower_select_query("billing_name", query, schema, foreign_keys)
    assert [item.attribute for item in plan.table("orders").relationships] == [
        "billing_customer_id"
    ]
    sql = SQLEmitter({"conversation": "conversation"}).emit(plan).source
    assert '"orders"."billing_customer_id" = "conversation"."customers"."customer_id"' in sql
    assert '"orders"."owner_id" = ' not in sql


def test_execution_record_is_request_local_and_cleared_with_its_context():
    descriptor = {"slug": "total_amount", "revision": 1, "dataset_version": "d1"}
    assert current_execution_record() is None
    with analysis_execution_context(descriptor, "c_" + "2" * 32):
        set_execution_record({"mode": "python"})
        assert current_execution_record() == {"mode": "python"}
    assert current_execution_record() is None


def test_multihop_enrichment_projects_only_the_declared_object():
    original = _plan()
    views = list(original.views)
    views[1] = EnrichedView(
        "total_amount_enriched",
        "total_amount_combined",
        (Enrichment("country", "orders", ("city", "country")),),
    )
    plan = AnalysisPlan(original.slug, original.tables, tuple(views))
    generated = SQLEmitter().emit(plan)
    enriched_sql = generated.statements[1]
    assert 'JOIN "knowledgebase"."city"' in enriched_sql
    assert 'JOIN "knowledgebase"."country"' in enriched_sql
    assert 'AS "city__qid"' not in enriched_sql
    assert 'AS "country__qid"' in enriched_sql
    assert "city__qid" not in plan.view_columns()["total_amount_enriched"]


def test_lowering_does_not_treat_a_repeated_foreign_id_as_row_identity():
    query = SelectQuery(
        select=(
            SelectItem(
                Aggregate(
                    "SUM",
                    ColumnRef("orders", "amount", SQLType.REAL),
                ),
                "total_amount",
            ),
        ),
        from_table="orders",
    )
    schema = [
        {
            "table": "orders",
            "name": "customer_id",
            "affinity": "INTEGER",
            "values": [1, 1],
        },
        {
            "table": "orders",
            "name": "amount",
            "affinity": "REAL",
            "values": [10, 20],
        },
    ]
    plan = lower_select_query("total_amount", query, schema, ())
    keys = {
        column.name for column in plan.table("orders").columns if column.primary_key
    }
    assert keys == {"customer_id", "amount"}
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.execute(text("ATTACH DATABASE ':memory:' AS conversation"))
        connection.execute(
            text(
                "CREATE TABLE conversation.orders "
                "(customer_id INTEGER NOT NULL, amount NUMERIC NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO conversation.orders VALUES (1, 10), (1, 20)")
        )
        result = DeterministicAnalysis(
            plan, conversation_schema="conversation"
        ).run(connection, mode="verify", estimated_rows=2)
    assert result.rows == ({"total_amount": 30},)


def test_plan_rejects_an_ambiguous_combined_relationship():
    original = _plan()
    source = original.table("orders")
    orders = TableSpec(
        source.name,
        source.class_name,
        source.attribute,
        source.schema,
        source.columns,
        source.relationships
        + (
            RelationshipSpec(
                "account_owner",
                "customers",
                ("customer_id",),
                ("customer_id",),
            ),
        ),
    )
    try:
        AnalysisPlan(
            original.slug,
            (orders,) + original.tables[1:],
            original.views,
        )
        raise AssertionError("an ambiguous combined join was accepted")
    except ValueError as exc:
        assert "exactly one relationship" in str(exc)


TESTS = [
    test_python_source_is_readable_object_graph_and_feed_forward_pipeline,
    test_both_emitters_are_byte_deterministic_and_have_matching_stages,
    test_generated_python_and_sql_execute_to_the_same_result,
    test_auto_policy_uses_python_only_below_the_configured_limit,
    test_development_debug_tree_contains_the_exact_executed_source,
    test_plan_rejects_a_stage_that_skips_its_predecessor,
    test_existing_typed_sql_ast_lowers_without_parsing_rendered_sql,
    test_lowering_uses_the_joined_relationship_when_two_edges_share_tables,
    test_execution_record_is_request_local_and_cleared_with_its_context,
    test_multihop_enrichment_projects_only_the_declared_object,
    test_lowering_does_not_treat_a_repeated_foreign_id_as_row_identity,
    test_plan_rejects_an_ambiguous_combined_relationship,
]


def main():
    failed = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(
        f"\ndeterministic emitters: {len(TESTS) - len(failed)} passed, {len(failed)} failed"
    )
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
