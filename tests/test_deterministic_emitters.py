"""Hermetic contract tests for the paired deterministic SQL/Python emitters."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, event, text

from engine.deterministic import (
    AggregateValue,
    AnalysisPlan,
    AntiJoinView,
    BinaryValue,
    CalculatedView,
    ColumnSpec,
    ColumnValue,
    CombinedView,
    CrossView,
    DeterministicAnalysis,
    EnrichedView,
    Enrichment,
    ExecutionMode,
    FilteredView,
    LiteralValue,
    MergeKey,
    PlanSection,
    PredicateValue,
    ProjectedView,
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
from engine.sql_rank import PoolSelection, SQLArbiter
from engine.deterministic.plan import JunctionValue
from engine.sql_ast import (
    Aggregate,
    BinaryExpr,
    ColumnRef,
    Comparison,
    Join,
    Literal,
    OrderTerm,
    SelectItem,
    SelectQuery,
    SQLType,
    Star,
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
        "from engine.deterministic.operators import EQ, MULTIPLY, SUM, View" in wrapper
    )
    assert "total_amount_enriched = total_amount_combined.for_each(" in wrapper
    assert "selectinload(Order.city).selectinload(City.country)" in wrapper
    assert "selectinload(Order.customer_id)" not in wrapper
    assert "set_committed_value(orders, 'customer_id', customers)" in wrapper
    assert 'lazy="raise"' in orders
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
    from engine.deterministic.runtime import execute_python

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
        generated = analysis.emit().python
        orm_statements = []

        def capture_orm_statement(_conn, _cursor, statement, _params, _ctx, _many):
            orm_statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture_orm_statement)
        python_result = execute_python(
            generated,
            connection,
            schema_map={"conversation": "conversation"},
            row_limit=10_000,
        )
        event.remove(engine, "before_cursor_execute", capture_orm_statement)
        first_order = python_result.views[0].rows[0].orders
        assert first_order.customer_id.name == "Ada"
        assert len(
            [sql for sql in orm_statements if sql.lstrip().upper().startswith("SELECT")]
        ) == 3

        orm_statements.clear()
        event.listen(engine, "before_cursor_execute", capture_orm_statement)
        python_only = analysis.run(
            connection, mode=ExecutionMode.PYTHON, estimated_rows=3
        )
        event.remove(engine, "before_cursor_execute", capture_orm_statement)
        transaction_statements = [
            sql.strip().upper()
            for sql in orm_statements
            if "SAVEPOINT" in sql.upper()
        ]
        assert sum(sql.startswith("SAVEPOINT") for sql in transaction_statements) == 1
        assert not any(
            sql.startswith("ROLLBACK TO SAVEPOINT")
            for sql in transaction_statements
        )
        assert python_only.rows == ({"total_amount": 40},)

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
    for kwargs in (
        {"estimated_rows": -1},
        {"estimated_rows": True},
        {"estimated_rows": 1, "python_row_limit": -1},
        {"estimated_rows": 1, "python_row_limit": False},
    ):
        try:
            choose_execution_mode("sql", **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid execution budget was accepted: {kwargs}")
    assert debug_generation_enabled("development")
    assert not debug_generation_enabled("production")
    assert debug_generation_enabled("production", explicit=True)


def test_auto_python_has_a_hard_materialized_row_limit_and_sql_fallback():
    from unittest.mock import patch

    table = TableSpec(
        "items",
        "Item",
        "items",
        "conversation",
        (ColumnSpec("id", "id", SQLType.INTEGER, primary_key=True, nullable=False),),
    )
    plan = AnalysisPlan(
        "items",
        (table,),
        (
            CombinedView("items_combined", ("items",)),
            ProjectedView(
                "items_result",
                "items_combined",
                (SelectedValue("id", ColumnValue("items", "id")),),
            ),
        ),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS conversation")
        connection.exec_driver_sql(
            "CREATE TABLE conversation.items (id INTEGER PRIMARY KEY)"
        )
        connection.exec_driver_sql(
            "INSERT INTO conversation.items VALUES (1), (2), (3)"
        )
        result = DeterministicAnalysis(
            plan, conversation_schema="conversation"
        ).run(
            connection,
            mode="auto",
            estimated_rows=2,
            python_row_limit=2,
        )
        assert result.mode is ExecutionMode.SQL
        assert result.rows == ({"id": 1}, {"id": 2}, {"id": 3})
        assert result.fallback_reason and "2-row limit" in result.fallback_reason

        from engine.deterministic.operators import PythonRowLimitExceeded

        try:
            DeterministicAnalysis(plan, conversation_schema="conversation").run(
                connection,
                mode="python",
                estimated_rows=2,
                python_row_limit=2,
            )
        except PythonRowLimitExceeded:
            pass
        else:
            raise AssertionError("explicit Python ignored its hard row limit")

        with patch(
            "engine.deterministic.service.execute_python",
            side_effect=RuntimeError("generated runtime failed"),
        ):
            recovered = DeterministicAnalysis(
                plan, conversation_schema="conversation"
            ).run(
                connection,
                mode="auto",
                estimated_rows=2,
                python_row_limit=3,
            )
        assert recovered.mode is ExecutionMode.SQL
        assert recovered.rows == ({"id": 1}, {"id": 2}, {"id": 3})
        assert recovered.fallback_reason == (
            "RuntimeError: generated runtime failed"
        )


def test_python_operators_preserve_sql_boolean_text_and_none_group_state():
    from engine.deterministic.operators import TEXT, View

    assert TEXT(True) == "true"
    assert TEXT(False) == "false"
    assert TEXT(None) is None

    grouped = View("source", ("a", "a")).group_reduce(
        "grouped",
        key=lambda row: row,
        initial=lambda _row: None,
        step=lambda current, _row: "seen" if current is None else "seen-again",
    )
    assert grouped.rows == ("seen-again",)
    empty_count = View("empty", (), row_limit=0).reduce(
        "count", initial=0, step=lambda current, _row: current + 1
    )
    assert empty_count.rows == (0,)


def test_plan_canonicalizes_numeric_literals_and_rejects_ambiguous_is():
    assert LiteralValue(0.1).value == Decimal("0.1")
    for value in (
        float("nan"),
        Decimal("Infinity"),
        datetime(2026, 9, 10, tzinfo=UTC),
    ):
        try:
            LiteralValue(value)
        except (TypeError, ValueError):
            pass
        else:
            raise AssertionError(f"unsupported literal was accepted: {value!r}")

    original = _plan()
    views = list(original.views)
    views[2] = FilteredView(
        "total_amount_filtered",
        "total_amount_enriched",
        PredicateValue(
            ColumnValue("country", "name"), "IS", LiteralValue("France")
        ),
    )
    try:
        AnalysisPlan(original.slug, original.tables, tuple(views))
    except ValueError as exc:
        assert "IS/IS NOT supports only NULL and boolean literals" in str(exc)
    else:
        raise AssertionError("ambiguous IS comparison was accepted")


def test_typed_date_literals_compare_as_dates_in_generated_python():
    from engine.deterministic.runtime import execute_python, materialized_python_views

    tables = [
        {
            "name": "events",
            "columns": ["event_id", "occurred"],
            "rows": [
                [1, "2025-12-31"],
                [2, "2026-01-01"],
                [3, "2026-01-02 03:04:05"],
            ],
        }
    ]
    schema = [
        {
            "table": "events",
            "name": "event_id",
            "affinity": "INTEGER",
            "values": [1, 2, 3],
        },
        {
            "table": "events",
            "name": "occurred",
            "affinity": "TEXT",
            "is_date": True,
            "values": [
                "2025-12-31",
                "2026-01-01",
                "2026-01-02 03:04:05",
            ],
        },
    ]
    query = SelectQuery(
        (
            SelectItem(
                Aggregate("COUNT", Star()),
                "count",
            ),
        ),
        "events",
        where=Comparison(
            ColumnRef("events", "occurred", SQLType.DATE),
            ">=",
            Literal("2026-01-01", SQLType.DATE),
        ),
    )
    plan = lower_select_query("recent", query, schema, ())
    assert plan.views[1].predicate.right.value == date(2026, 1, 1)

    from sqlalchemy.pool import StaticPool

    from spider.probe.evalutil import build_mem_db

    raw = build_mem_db(tables)
    engine = create_engine(
        "sqlite+pysqlite://", creator=lambda: raw, poolclass=StaticPool
    )
    try:
        with engine.connect() as connection:
            generated = PythonEmitter().emit(plan)
            result = execute_python(
                generated,
                connection,
                schema_map={"conversation": "main"},
                row_limit=10_000,
            )
            # The one-sheet entry is read inside the filter stage, the first materialized one.
            kept = materialized_python_views(result, plan)[0]
            assert [row["events__occurred"] for row in kept] == [
                "2026-01-01",
                "2026-01-02 03:04:05",
            ]
            assert materialized_python_views(result, plan)[-1] == ({"count": 2},)
    finally:
        engine.dispose()


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


def test_plan_accepts_a_prior_dependency_and_rejects_a_forward_reference():
    plan = _plan()
    views = list(plan.views)
    views[3] = CalculatedView(
        "total_amount_calculated",
        "total_amount_combined",
        (SelectedValue("gross_amount", ColumnValue("orders", "amount")),),
    )
    branched = AnalysisPlan(plan.slug, plan.tables, tuple(views))
    assert branched.views[3].source == "total_amount_combined"
    views[3] = CalculatedView(
        "total_amount_calculated",
        "total_amount_total",
        (SelectedValue("gross_amount", ColumnValue("orders", "amount")),),
    )
    try:
        AnalysisPlan(plan.slug, plan.tables, tuple(views))
        raise AssertionError("a forward view reference was accepted")
    except ValueError as exc:
        assert "input must precede" in str(exc)


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


def test_spider_scalar_gold_runner_executes_the_selected_ast_with_python():
    from engine.sql_candidate import ScoredQuery
    from spider.probe.full_eval import (
        _execute_python_candidate,
        _lower_python_candidate,
    )
    from spider.probe.spider_eval import compare

    tables = [
        {
            "name": "orders",
            "columns": ["order_id", "amount"],
            "rows": [[1, 10], [2, 20], [3, None]],
        }
    ]
    schema = [
        {
            "table": "orders",
            "name": "order_id",
            "affinity": "INTEGER",
            "values": [1, 2, 3],
        },
        {
            "table": "orders",
            "name": "amount",
            "affinity": "INTEGER",
            "values": [10, 20, None],
        },
    ]
    query = SelectQuery(
        (
            SelectItem(
                Aggregate("SUM", ColumnRef("orders", "amount", SQLType.INTEGER)),
                "total",
            ),
        ),
        "orders",
    )
    candidate = ScoredQuery(
        query, "SELECT SUM(amount) AS total FROM orders", 1, ()
    )
    plan, estimated_rows = _lower_python_candidate(candidate, tables, schema, ())
    rows = _execute_python_candidate(plan, tables, estimated_rows, 10_000)
    assert rows == [[30]]
    assert compare([[30]], rows)["scalar_exact"] is True

    from spider.probe.full_eval import ast_predict

    class FakeEncoder:
        def ingest(self, input_tables):
            return input_tables, ()

        def schema(self, _tables, _foreign_keys):
            return schema, {}, {}

        sql_arbiter = SQLArbiter.load(
            Path(__file__).resolve().parents[1] / "engine" / "data" / "sql_arbiter.json")

        def select_query(self, *_args):
            return PoolSelection((candidate,), frozenset(), (True,), (True,), ((-1.0, 1),), (0.0,),
                                 (0,), 0, 1)

        def guard(self, _sql):
            return True, None

        def execute(self, _table_map, _schema, _sql):
            return ["total"], [(30,)]

    evaluated = ast_predict(
        FakeEncoder(),
        tables,
        "total amount",
        schema_fks=(),
        execution_backend="auto",
        python_row_limit=10_000,
    )
    assert evaluated["ok"] is True
    assert evaluated["rows"] == [[30]]
    assert evaluated["execution_backend_actual"] == "python"
    assert evaluated["python_sql_equal"] is True


def test_auto_grades_the_served_python_answer_when_a_limit_cutoff_ties():
    """Gold grading must observe serving, not silently substitute a SQL answer.

    Ties may be underdetermined, but that is a reported divergence, not a reason
    to change which backend's output the evaluator grades. Verify fails hard.
    """
    from engine.sql_candidate import ScoredQuery
    from spider.probe.full_eval import ast_predict

    tables = [
        {
            "name": "courses",
            "columns": ["course_id", "course_name"],
            "rows": [[1, "rs"], [2, "ai"]],
        }
    ]
    schema = [
        {"table": "courses", "name": "course_id", "affinity": "INTEGER", "values": [1, 2]},
        {"table": "courses", "name": "course_name", "affinity": "TEXT", "values": ["rs", "ai"]},
    ]
    name = ColumnRef("courses", "course_name", SQLType.TEXT)
    query = SelectQuery(
        (
            SelectItem(name, "course_name"),
            SelectItem(Aggregate("COUNT", Star()), "total"),
        ),
        "courses",
        group_by=(name,),
        order_by=(OrderTerm(Aggregate("COUNT", Star()), "DESC"),),
        limit=1,
    )
    candidate = ScoredQuery(
        query,
        'SELECT course_name, COUNT(*) FROM courses GROUP BY course_name '
        "ORDER BY COUNT(*) DESC LIMIT 1",
        1,
        (),
    )

    class TiedEncoder:
        def ingest(self, input_tables):
            return input_tables, ()

        def schema(self, _tables, _foreign_keys):
            return schema, {}, {}

        sql_arbiter = SQLArbiter.load(
            Path(__file__).resolve().parents[1] / "engine" / "data" / "sql_arbiter.json")

        def select_query(self, *_args):
            return PoolSelection((candidate,), frozenset(), (True,), (True,), ((-1.0, 1),), (0.0,),
                                 (0,), 0, 1)

        def guard(self, _sql):
            return True, None

        def execute(self, _table_map, _schema, _sql):
            # The engine-arbitrary pick: the OTHER tie member than the plan's
            # deterministic tie-break would choose.
            return ["course_name", "total"], [("rs", 1)]

    evaluated = ast_predict(
        TiedEncoder(), tables, "course with most enrollments",
        schema_fks=(), execution_backend="auto", python_row_limit=10_000,
    )
    assert evaluated["ok"] is True
    assert evaluated["rows"] == [["ai", 1]]                 # exactly what AUTO serves
    assert evaluated["execution_backend_actual"] == "python"
    assert evaluated["python_sql_equal"] is False           # the divergence is recorded
    assert not evaluated.get("python_fallback_reason")

    verified = ast_predict(
        TiedEncoder(), tables, "course with most enrollments",
        schema_fks=(), execution_backend="verify", python_row_limit=10_000,
    )
    assert verified["ok"] is False                          # verify never papers over it


def test_lowering_uses_the_joined_relationship_when_two_edges_share_tables():
    query = SelectQuery(
        select=(SelectItem(ColumnRef("customers", "name", SQLType.TEXT), "name"),),
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
    for column in schema:
        column["values"] = [1] if column["affinity"] == "INTEGER" else ["Ada"]
    plan = lower_select_query("billing_name", query, schema, foreign_keys)
    assert [item.attribute for item in plan.table("orders").relationships] == [
        "billing_customer_id"
    ]
    sql = SQLEmitter({"conversation": "conversation"}).emit(plan).source
    assert (
        '"orders"."billing_customer_id" = "conversation"."customers"."customer_id"'
        in sql
    )
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


def test_enrichment_reroots_through_an_object_already_loaded_by_combined():
    original = _plan()
    customers = replace(
        original.table("customers"),
        columns=original.table("customers").columns
        + (ColumnSpec("country", "country", SQLType.TEXT),),
        relationships=(
            RelationshipSpec("country", "country", ("country",), ("qid",)),
        ),
    )
    plan = AnalysisPlan(
        "customer_country",
        (original.table("orders"), customers, *original.tables[2:]),
        (
            CombinedView(
                "customer_country_combined", ("orders", "customers")
            ),
            EnrichedView(
                "customer_country_enriched",
                "customer_country_combined",
                (
                    Enrichment(
                        "country",
                        "orders",
                        ("customer_id", "country"),
                    ),
                ),
            ),
            ProjectedView(
                "customer_country_result",
                "customer_country_enriched",
                (
                    SelectedValue(
                        "country", ColumnValue("country", "name")
                    ),
                ),
            ),
        ),
    )
    wrapper = PythonEmitter().emit(plan).files["orders_customers.py"]
    assert "selectinload(Customer.country)" in wrapper
    assert "selectinload(Order.customer_id)" not in wrapper


def test_relationship_join_predicates_cannot_capture_unrelated_tables():
    original = _plan()
    orders = original.table("orders")
    invalid_customer = replace(
        orders.relationship("customer_id"),
        condition=PredicateValue(
            ColumnValue("orders", "customer_id"),
            "=",
            ColumnValue("city", "qid"),
        ),
    )
    orders = replace(
        orders,
        relationships=(invalid_customer, orders.relationship("city")),
    )
    try:
        AnalysisPlan(original.slug, (orders,) + original.tables[1:], original.views)
    except ValueError as exc:
        assert "table 'city' is unavailable" in str(exc)
    else:
        raise AssertionError("relationship captured an unrelated table")


def test_custom_orm_join_uses_python_spelling_for_sql_is_predicates():
    original = _plan()
    orders = original.table("orders")
    customer = orders.relationship("customer_id")
    custom_customer = replace(
        customer,
        condition=JunctionValue(
            "AND",
            (
                PredicateValue(
                    BinaryValue(
                        ColumnValue("orders", "customer_id"),
                        "+",
                        LiteralValue(0),
                    ),
                    "=",
                    ColumnValue("customers", "customer_id"),
                ),
                PredicateValue(
                    ColumnValue("customers", "name"),
                    "IS NOT",
                    LiteralValue(None),
                ),
            ),
        ),
    )
    orders = replace(
        orders,
        relationships=(custom_customer, orders.relationship("city")),
    )
    plan = replace(original, tables=(orders, *original.tables[1:]))
    package = PythonEmitter().emit(plan)
    source = package.files["orders.py"]
    assert "Customer.name != None" in source
    assert "Customer.name IS NOT None" not in source

    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        for schema in ("conversation", "knowledgebase"):
            connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {schema}")
        connection.exec_driver_sql(
            "CREATE TABLE conversation.orders "
            "(order_id INTEGER, customer_id INTEGER, amount NUMERIC, city TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE conversation.customers (customer_id INTEGER, name TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE knowledgebase.city (qid TEXT, name TEXT, country TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE knowledgebase.country (qid TEXT, name TEXT)"
        )
        connection.exec_driver_sql(
            "INSERT INTO conversation.customers VALUES (1, 'Ada')"
        )
        connection.exec_driver_sql(
            "INSERT INTO conversation.orders VALUES (10, 1, 12.5, 'Q90')"
        )
        connection.exec_driver_sql(
            "INSERT INTO knowledgebase.city VALUES ('Q90', 'Paris', 'Q142')"
        )
        connection.exec_driver_sql(
            "INSERT INTO knowledgebase.country VALUES ('Q142', 'France')"
        )
        result = DeterministicAnalysis(
            plan, conversation_schema="conversation"
        ).run(connection, mode="verify", estimated_rows=1)
    assert result.rows == ({"total_amount": Decimal("25.00000000000000000000")},)


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
        result = DeterministicAnalysis(plan, conversation_schema="conversation").run(
            connection, mode="verify", estimated_rows=2
        )
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


def _execute_fixture(plan, statements, mode="verify", estimated_rows=3):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS conversation")
            connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS knowledgebase")
            for statement in statements:
                connection.exec_driver_sql(statement)
            return DeterministicAnalysis(plan, conversation_schema="conversation").run(
                connection,
                mode=mode,
                estimated_rows=estimated_rows,
            )
    finally:
        engine.dispose()


def test_full_results_are_independent_of_trace_preview_limit():
    table = TableSpec(
        "items",
        "Item",
        "items",
        "conversation",
        (ColumnSpec("id", "id", SQLType.INTEGER, primary_key=True, nullable=False),),
    )
    plan = AnalysisPlan(
        "ids",
        (table,),
        (
            CombinedView("ids_combined", ("items",)),
            ProjectedView(
                "ids_result",
                "ids_combined",
                (SelectedValue("id", ColumnValue("items", "id")),),
            ),
        ),
    )
    statements = [
        "CREATE TABLE conversation.items (id INTEGER PRIMARY KEY)",
        "INSERT INTO conversation.items VALUES "
        + ",".join(f"({i})" for i in range(75)),
    ]
    for mode in ("sql", "python", "verify"):
        result = _execute_fixture(plan, statements, mode, 75)
        assert len(result.rows) == 75, mode
        assert len(result.record()["views"][-1]["rows"]) == 50


def test_composite_relationship_executes_and_preserves_both_join_keys():
    parent = TableSpec(
        "parents",
        "Parent",
        "parents",
        "conversation",
        (
            ColumnSpec("id", "id", SQLType.INTEGER, primary_key=True, nullable=False),
            ColumnSpec(
                "region", "region", SQLType.INTEGER, primary_key=True, nullable=False
            ),
            ColumnSpec("label", "label", SQLType.TEXT),
        ),
    )
    child = TableSpec(
        "children",
        "Child",
        "children",
        "conversation",
        (
            ColumnSpec("id", "id", SQLType.INTEGER, primary_key=True, nullable=False),
            ColumnSpec("parent_id", "parent_id", SQLType.INTEGER),
            ColumnSpec("region", "region", SQLType.INTEGER),
        ),
        (
            RelationshipSpec(
                "parent", "parents", ("parent_id", "region"), ("id", "region")
            ),
        ),
    )
    plan = AnalysisPlan(
        "labels",
        (child, parent),
        (
            CombinedView("labels_combined", ("children", "parents")),
            ProjectedView(
                "labels_result",
                "labels_combined",
                (SelectedValue("label", ColumnValue("parents", "label")),),
            ),
        ),
    )
    result = _execute_fixture(
        plan,
        [
            "CREATE TABLE conversation.parents (id INTEGER, region INTEGER, label TEXT)",
            "CREATE TABLE conversation.children (id INTEGER, parent_id INTEGER, region INTEGER)",
            "INSERT INTO conversation.parents VALUES (1,1,'west'), (1,2,'east')",
            "INSERT INTO conversation.children VALUES (10,1,2)",
        ],
    )
    assert result.rows == ({"label": "east"},)


def test_multihop_missing_references_and_prior_calculations_match_sql():
    original = _plan()
    orders = replace(
        original.table("orders"),
        relationships=(original.table("orders").relationship("city"),),
    )
    plan = AnalysisPlan(
        "amount",
        (orders, original.table("city"), original.table("country")),
        (
            CombinedView("amount_combined", ("orders",)),
            CalculatedView(
                "amount_calculated",
                "amount_combined",
                (SelectedValue("gross", ColumnValue("orders", "amount")),),
            ),
            EnrichedView(
                "amount_enriched",
                "amount_calculated",
                (Enrichment("country", "orders", ("city", "country")),),
            ),
            ReducedView(
                "amount_total",
                "amount_enriched",
                (AggregateValue("total", "SUM", ViewValue("gross")),),
            ),
        ),
    )
    result = _execute_fixture(
        plan,
        [
            "CREATE TABLE conversation.orders (order_id INTEGER, customer_id INTEGER, amount NUMERIC, city TEXT)",
            "CREATE TABLE knowledgebase.city (qid TEXT, name TEXT, country TEXT)",
            "CREATE TABLE knowledgebase.country (qid TEXT, name TEXT)",
            "INSERT INTO conversation.orders VALUES (1,1,10,'Paris'), (2,1,20,'missing'), (3,1,40,'orphan')",
            "INSERT INTO knowledgebase.city VALUES ('Paris','Paris','FR'), ('orphan','Unknown','missing')",
            "INSERT INTO knowledgebase.country VALUES ('FR','France')",
        ],
    )
    assert result.rows == ({"total": 10},)
    # The one-sheet entry is read inside the calculated stage; it is not a stage of its own.
    assert result.emission.sql.manifest["views"] == [
        "amount_calculated", "amount_enriched", "amount_total",
    ]
    assert [len(rows) for rows in result.view_rows] == [3, 1, 1]


def test_direct_execution_context_and_explicit_mode_fail_closed():
    from engine.deterministic.context import (
        current_analysis_context,
        enforce_execution_response,
    )
    from mcp_server.engine_client import shape_reason_response

    with analysis_execution_context(None, "c_" + "4" * 32, execution_mode="python"):
        assert current_analysis_context().slug == "query"
        assert current_analysis_context().execution_mode == "python"
        set_execution_record({"mode": "python"})
        with analysis_execution_context(None, "c_" + "5" * 32):
            assert current_analysis_context().slug == "query"
            assert current_analysis_context().execution_mode is None
            assert current_execution_record() is None
        assert current_execution_record() == {"mode": "python"}
    answer = {"result": {"columns": ["n"], "rows": [[1]]}}
    for mode in ("python", "verify"):
        rejected = enforce_execution_response(answer, mode)
        assert rejected.get("error") and "result" not in rejected
        assert rejected["execution"]["verified"] is False
        assert shape_reason_response(rejected, None)["status"] == "error"
    for mode in (None, "sql", "auto"):
        allowed = enforce_execution_response(answer, mode)
        assert allowed["execution"]["actual"] == "sql"
        assert allowed["execution"]["implementation"] == "sql_executor"
        assert allowed["execution"]["fallback_reason"] is None
    recovered = enforce_execution_response(
        {
            **answer,
            "deterministic": {
                "mode": "sql",
                "fallback_reason": "PythonRowLimitExceeded: bounded",
            },
        },
        "auto",
    )
    assert recovered["execution"]["fallback_reason"].endswith("bounded")
    verified = enforce_execution_response(
        {**answer, "deterministic": {"mode": "verify"}}, "verify"
    )
    shaped = shape_reason_response(verified, None)
    assert shaped["execution"]["verified"] is True
    assert shaped["deterministic"]["mode"] == "verify"
    assert current_analysis_context() is None and current_execution_record() is None


def test_parity_never_rounds_away_large_decimal_differences():
    from engine.deterministic.runtime import VerificationMismatch, assert_equivalent

    left = Decimal(1234567890123456789012345678901234567890)
    right = Decimal(1234567890123456789012345678901234567891)
    try:
        assert_equivalent(({"n": left},), ({"n": right},))
    except VerificationMismatch:
        pass
    else:
        raise AssertionError("parity hid a precision mismatch")
    assert_equivalent(
        ({"n": Decimal("1.00")}, {"n": Decimal("1.1")}),
        ({"n": Decimal("1.10")}, {"n": 1}),
    )


def test_lowering_requires_identity_evidence_after_numeric_coercion():
    from engine.deterministic.lower import UnsupportedDeterministicPlan

    query = SelectQuery(
        (SelectItem(ColumnRef("items", "id", SQLType.INTEGER)),), "items"
    )
    for values in (None, ["01", 1], [None, 1]):
        schema = [{"table": "items", "name": "id", "affinity": "INTEGER"}]
        if values is not None:
            schema[0]["values"] = values
        try:
            lower_select_query("ids", query, schema, ())
        except UnsupportedDeterministicPlan:
            pass
        else:
            raise AssertionError(f"unproven identity accepted: {values}")


def test_grouped_projection_keeps_select_order_when_group_by_order_differs():
    a, b = (
        ColumnRef("items", "a", SQLType.INTEGER),
        ColumnRef("items", "b", SQLType.INTEGER),
    )
    query = SelectQuery(
        (
            SelectItem(b, "b"),
            SelectItem(a, "a"),
            SelectItem(Aggregate("COUNT", Star()), "n"),
        ),
        "items",
        group_by=(a, b),
    )
    schema = [
        {"table": "items", "name": name, "affinity": "INTEGER", "values": values}
        for name, values in (("a", [1, 2]), ("b", [10, 20]))
    ]
    plan = lower_select_query("groups", query, schema, ())
    assert plan.view_columns()["groups_total"] == ("b", "a", "n")
    result = _execute_fixture(
        plan,
        [
            "CREATE TABLE conversation.items (a INTEGER, b INTEGER)",
            "INSERT INTO conversation.items VALUES (1,10),(2,20)",
        ],
    )
    assert list(result.rows[0]) == ["b", "a", "n"]


def test_integer_division_uses_decimal_and_sql_null_semantics():
    from engine.deterministic.operators import DIVIDE, AverageState

    assert DIVIDE(1, 2) == Decimal("0.5")
    assert DIVIDE(1, 3) == Decimal("0.33333333333333333333")
    assert DIVIDE(1, 0) is None and DIVIDE(None, 1) is None
    assert AverageState().value is None
    assert AverageState(Decimal(1), 3).value == DIVIDE(1, 3)


def test_mutating_constructor_lists_cannot_change_an_emitted_plan():
    original = _plan()
    tables, views = list(original.tables), list(original.views)
    plan = AnalysisPlan(original.slug, tables, views)
    source = PythonEmitter().emit(plan).source_sha256
    tables.clear()
    views.clear()
    assert PythonEmitter().emit(plan).source_sha256 == source


def test_collection_enrichment_is_rejected_before_generation():
    original = _plan()
    orders = original.table("orders")
    orders = replace(
        orders,
        relationships=tuple(
            replace(edge, many=True) if edge.attribute == "city" else edge
            for edge in orders.relationships
        ),
    )
    try:
        AnalysisPlan(original.slug, (orders,) + original.tables[1:], original.views)
    except ValueError as exc:
        assert "scalar relationships" in str(exc)
    else:
        raise AssertionError("unsupported collection traversal was accepted")


def test_collection_combined_relationship_is_rejected_before_generation():
    original = _plan()
    orders = original.table("orders")
    orders = replace(
        orders,
        relationships=tuple(
            replace(edge, many=True) if edge.attribute == "customer_id" else edge
            for edge in orders.relationships
        ),
    )
    try:
        AnalysisPlan(original.slug, (orders,) + original.tables[1:], original.views)
    except ValueError as exc:
        assert "combined views currently require scalar relationships" in str(exc)
    else:
        raise AssertionError("unsupported collection join was accepted")


def test_colliding_generated_class_cannot_replace_the_result_type():
    original = _plan()
    orders = replace(original.table("orders"), class_name="AnalysisResult")
    plan = AnalysisPlan(original.slug, (orders,) + original.tables[1:], original.views)
    try:
        PythonEmitter().emit(plan)
    except ValueError as exc:
        assert "names collide" in str(exc)
    else:
        raise AssertionError("a generated class silently replaced AnalysisResult")


def test_knowledge_delegate_preserves_shared_plan_execution_evidence():
    from types import SimpleNamespace

    from engine.deterministic.context import enforce_execution_response
    from engine.knowledge_tables import KnowledgeTableQuery

    raw = {
        "result": {"columns": ["total"], "rows": [[3]]},
        "sql": 'SELECT SUM(amount) FROM "query_combined"',
        "views": [{"name": "query_total", "rows": [[3]]}],
        "deterministic": {"mode": "verify", "python": {"source_sha256": "fixture"}},
    }
    # Exercise the actual serving adapter with only external planning/lookup inputs stubbed.
    adapter = SimpleNamespace(
        q11=SimpleNamespace(
            ingest=lambda tables: ([], []),
            schema=lambda *args: ([], {}, {}),
            serve=lambda *args: raw,
        ),
        read_op_all=lambda *args: None,
        _currency_conversion_binding=lambda *args: None,
        _world_rate_binding=lambda *args: None,
        meaning_filter=lambda *args: None,
        _own_value_matches=lambda *args: [],
        world_target=lambda *args: None,
        _debug_input=lambda *args: {},
    )
    response = KnowledgeTableQuery.serve(
        adapter, [], "total amount", as_of="2026-09-10"
    )
    assert response["views"] is raw["views"]
    assert response["deterministic"] is raw["deterministic"]
    assert (
        enforce_execution_response(response, "verify")["execution"]["verified"] is True
    )


def test_coverage_checks_filters_in_the_full_emitted_program():
    from types import SimpleNamespace
    from unittest.mock import patch

    import engine.knowledge_query as knowledge_query
    from engine.knowledge_query import KnowledgeQuery, _coverage_sql

    program = SQLEmitter().emit(_plan())
    response = {
        "sql": program.statements[-1],
        "deterministic": {"sql": program.record()},
    }
    adapter = SimpleNamespace(
        _encode=lambda words: [[0] for word in words],
        _word_qid=lambda word: None,
        _phrase_qids=lambda phrases: {},
        _best_world_entity=lambda words: (words[0], "France", "country", 1),
    )
    schema = [{"table": "orders", "name": "amount", "affinity": "REAL"}]
    # This unit isolates coverage over already-emitted SQL. POS tagging is an
    # independent production dependency and is tested through the full runtime.
    with patch.object(knowledge_query, "closed_class_words", return_value=frozenset()):
        assert KnowledgeQuery._uncovered(
            adapter, "total amount in France", schema, response["sql"]
        ) == ["france"]
        assert (
            KnowledgeQuery._uncovered(
                adapter, "total amount in France", schema, _coverage_sql(response)
            )
            == []
        )
        assert _coverage_sql({"sql": "SELECT 1"}) == "SELECT 1"


def test_coverage_prose_is_not_a_place_or_an_ignored_status():
    from types import SimpleNamespace
    from unittest.mock import patch
    import engine.knowledge_query as knowledge_query
    from engine.knowledge_query import KnowledgeQuery
    adapter = SimpleNamespace(
        _encode=lambda words: [[0] for _ in words],
        _word_qid=lambda word: None,
        _phrase_qids=lambda phrases: {},
        _best_world_entity=lambda words: (words[0], "Spurious Place", "city", 0.61),
    )
    schema = [{"table": "payments", "name": "amount", "affinity": "REAL", "values": [10]}]
    def dropped(question, columns=schema):
        return KnowledgeQuery._uncovered(adapter, question, columns, 'SELECT SUM(amount) FROM payments')
    with patch.object(knowledge_query, "closed_class_words", return_value=frozenset()):
        assert dropped('How many payments are listed?') == []
        assert dropped('What is the total amount paid?') == []
        assert dropped('What is the total amount paid in France?') == ['france']
        assert dropped('What is the total amount among payments in France?') == ['france']
        assert dropped('How many listed payments?') == ['listed']  # status adjective, not display prose
        with_status = schema + [{"table": "payments", "name": "status", "values": ["paid", "listed"]}]
        assert dropped('What is the total amount paid?', with_status) == ['paid']
        assert dropped('How many payments are listed?', with_status) == ['listed']
        availability = schema + [{'table': 'payments', 'name': 'availability', 'values': ['Listed for sale']}]
        assert dropped('How many payments are listed?', availability) == ['listed']
        pending = schema + [{'table': 'payments', 'name': 'notes', 'values': ['Unpaid']}]
        assert dropped('What is the total amount paid?', pending) == ['paid']
        assert dropped('What is the total amount paid?', [dict(schema[0], table='invoices')]) == ['paid']


def test_distinct_count_operator_and_sheet_scope_are_covered():
    from types import SimpleNamespace
    from unittest.mock import patch
    import engine.knowledge_query as knowledge_query
    from engine.knowledge_query import KnowledgeQuery
    adapter = SimpleNamespace(
        _encode=lambda words: [[0] for _ in words],
        _word_qid=lambda word: None,
        _phrase_qids=lambda phrases: {},
        _best_world_entity=lambda words: (words[0], 'Spurious Place', 'city', 0.61),
    )
    schema = [
        {"table": "customers", "name": "order ID", "affinity": "INTEGER", "values": [101, 102]},
        {"table": "customers", "name": "customer", "affinity": "TEXT", "values": ["Holmes"]},
    ]
    sql = 'SELECT COUNT(DISTINCT "customers"."order ID") FROM "customers"'
    question = ("Count the unique values in the 'order ID' column across all data rows. "
                "Return the count and show the calculation steps.")
    with patch.object(knowledge_query, "closed_class_words", return_value=frozenset()):
        assert KnowledgeQuery._uncovered(adapter, question, schema, sql) == []
        assert KnowledgeQuery._uncovered(adapter, question + " In France.", schema, sql) == ['france']



def test_a_verb_or_adverb_says_what_the_rows_did_not_which_rows():
    """Chrome exploration (2026-10-01): half of a dozen natural questions were declined because a word that
    describes the rows sat near some town: "which item sold the most units" (sold), "how many documents are
    still pending" (still), "how many deliveries weigh more than 3 kg" (weigh), "how many leads came from
    France" (came, which also hid the counted noun), "what is the average score of the leads" (leads), "how
    many payments were made by card" (made), "what was the most expensive event" (expensive), "what is the total
    quantity purchased" (purchased) and "which category brought in the most revenue" (revenue). A finite verb,
    a light verb's participle or an adverb is covered, and so are the rows an aggregate is taken over, an
    adjective an ordering grades, a participle on the measured column and a measure word an aggregate
    realizes; a participle that names a state of the rows, and a verb the data holds as a value, are still row
    filters."""
    from types import SimpleNamespace
    from unittest.mock import patch
    import engine.knowledge_query as knowledge_query
    from engine.knowledge_query import KnowledgeQuery

    adapter = SimpleNamespace(
        _encode=lambda words: [[0] for _ in words],
        _word_qid=lambda word: None,
        _phrase_qids=lambda phrases: {},
        _best_world_entity=lambda words: (words[0], "Spurious Place", "city", 0.61),
    )
    orders = [{"table": "orders", "name": "item", "affinity": "TEXT", "values": ["Baguette", "Croissant Box"]},
              {"table": "orders", "name": "quantity", "affinity": "INTEGER", "values": [1, 2]},
              {"table": "orders", "name": "unit price", "affinity": "REAL", "values": [3, 6]}]
    intake = [{"table": "intake", "name": "status", "affinity": "TEXT", "values": ["Signed", "Pending"]}]
    leads = [{"table": "responses", "name": "country", "affinity": "TEXT", "values": ["France", "Germany"]}]
    deliveries = [{"table": "deliveries", "name": "weight kg", "affinity": "REAL", "values": [2, 5]}]
    top_item = 'SELECT item FROM orders GROUP BY item ORDER BY SUM(quantity) DESC LIMIT 1'

    def dropped(question, schema, sql):
        return KnowledgeQuery._uncovered(adapter, question, schema, sql)

    # The production tagger reads both the closed-class words and the verbs here: "are" ends the counted
    # noun phrase in "how many documents are still pending" as it does in serving.
    with patch.object(knowledge_query, "closed_class_words", wraps=knowledge_query.closed_class_words):
        assert dropped("Which item sold the most units?", orders, top_item) == []
        assert dropped("How many documents are still pending?", intake,
                       "SELECT COUNT(*) FROM intake WHERE status = 'Pending'") == []
        assert dropped("Which delivery weighs the most?", deliveries,
                       'SELECT "delivery ID" FROM deliveries ORDER BY "weight kg" DESC LIMIT 1') == []
        assert dropped("How many deliveries weigh more than 3 kg?", deliveries,
                       'SELECT COUNT(*) FROM deliveries WHERE "weight kg" > 3') == []
        assert dropped("How many leads came from France?", leads,
                       "SELECT COUNT(*) FROM responses WHERE country = 'France'") == []
        # A participle names a state of the rows: an unfiltered count of returned orders is declined.
        assert dropped("How many orders were returned?", orders, "SELECT COUNT(*) FROM orders") == ["returned"]
        # A verb the data holds as a value is a row filter too.
        sold = orders + [{"table": "orders", "name": "status", "affinity": "TEXT", "values": ["Sold", "Open"]}]
        assert dropped("Which item sold the most units?", sold, top_item) == ["sold"]
        # A place the query did not filter on is still dropped.
        assert dropped("How many leads came from Spain?", leads,
                       "SELECT COUNT(*) FROM responses WHERE country = 'France'") == ["spain"]
        # The noun a ranking by the count takes the most or fewest of is what COUNT counts: "which country has
        # the most leads" was declined over 'leads'. Ranked by another aggregate it is still checked, and so is
        # a word before it.
        by_count = "SELECT country FROM responses GROUP BY country ORDER BY COUNT(*) {} LIMIT 1"
        assert dropped("Which country has the most leads?", leads, by_count.format("DESC")) == []
        assert dropped("Which country has the fewest leads?", leads, by_count.format("ASC")) == []
        ranked_score = "SELECT country FROM responses GROUP BY country ORDER BY SUM(score) DESC LIMIT 1"
        assert dropped("Which country has the most leads?", leads, ranked_score) == ["leads"]
        assert dropped("Which country has the most German leads?", leads, by_count.format("DESC")) == ["german"]
        # The rows an aggregate is taken over are covered like a counted noun; a word before them is not.
        scored = leads + [{"table": "responses", "name": "score", "affinity": "INTEGER", "values": [74, 86]}]
        assert dropped("What is the average score of the leads?", scored,
                       "SELECT AVG(score) FROM responses") == []
        assert dropped("What is the average score of the German leads?", scored,
                       "SELECT AVG(score) FROM responses") == ["german"]
        assert dropped("What is the average score of the leads in Spain?", scored,
                       "SELECT AVG(score) FROM responses") == ["spain"]
        # A light verb's participle names no state; another participle still does.
        payments = [{"table": "payments", "name": "payment_instrument", "affinity": "TEXT",
                     "values": ["card", "bank transfer"]},
                    {"table": "payments", "name": "amount", "affinity": "REAL", "values": [120, 300]}]
        assert dropped("How many payments were made by card?", payments,
                       "SELECT COUNT(*) FROM payments WHERE payment_instrument = 'card'") == []
        assert dropped("How many payments were refunded?", payments, "SELECT COUNT(*) FROM payments") == ["refunded"]
        # A graded adjective is realized by the ordering; without one it is still dropped.
        events = [{"table": "catering", "name": "event", "affinity": "TEXT", "values": ["Wine Dinner", "Gala"]},
                  {"table": "catering", "name": "amount", "affinity": "REAL", "values": [5200, 3900]}]
        assert dropped("What was the most expensive event?", events,
                       "SELECT event FROM catering ORDER BY amount DESC LIMIT 1") == []
        assert dropped("Which is the cheapest event?", events,
                       "SELECT event FROM catering ORDER BY amount ASC LIMIT 1") == []
        assert dropped("What was the most expensive event?", events, "SELECT event FROM catering") == ["expensive"]
        # A participle on the measured column says how its values came about.
        purchases = [{"table": "purchases", "name": "quantity", "affinity": "INTEGER", "values": [1, 2]}]
        assert dropped("What is the total quantity purchased?", purchases,
                       "SELECT SUM(quantity) FROM purchases") == []

    # A measure word the aggregate realizes is covered before any town it sits near; without an aggregate it
    # is still a dropped measure, and a place the query never filtered is still dropped.
    import numpy as np

    measured = SimpleNamespace(
        _encode=lambda words: np.array([[1.0, 0.0] if word == "revenue" else [0.0, 1.0] for word in words]),
        _word_qid=lambda word: None,
        _phrase_qids=lambda phrases: {},
        _best_world_entity=lambda words: (words[0], "Spurious Place", "city", 0.61),
    )
    sales = [{"table": "purchases", "name": "category", "affinity": "TEXT", "values": ["Office", "Home"]},
             {"table": "purchases", "name": "line_total", "affinity": "REAL", "values": [100, 50],
              "qvec": [1.0, 0.0]}]
    ranked = "SELECT category FROM purchases GROUP BY category ORDER BY SUM(line_total) DESC LIMIT 1"
    with patch.object(knowledge_query, "closed_class_words", wraps=knowledge_query.closed_class_words):
        assert KnowledgeQuery._uncovered(
            measured, "Which category brought in the most revenue?", sales, ranked) == []
        assert KnowledgeQuery._uncovered(
            measured, "Which category has revenue?", sales, "SELECT category FROM purchases") == ["revenue"]
        assert KnowledgeQuery._uncovered(
            measured, "Which category brought in the most revenue in France?", sales, ranked) == ["france"]



def test_a_place_or_world_type_the_query_never_realized_is_dropped():
    """Chrome exploration (2026-10-01) on the bank, restaurant and hospital sheets: "which country has the most
    deposits" ranked the banks, "total amount by country" summed every restaurant into one total, and "which bank
    has the most deposits in Europe" ranked every bank in the world. A world type the question asks for is
    dropped unless a column of that name or the query realizes it, and so is a place named exactly that the
    query never filtered on, continents included."""
    from types import SimpleNamespace
    from unittest.mock import patch
    import engine.knowledge_query as knowledge_query
    from engine.knowledge_query import KnowledgeQuery

    adapter = SimpleNamespace(
        _encode=lambda words: [[0] for _ in words],
        _word_qid=lambda word: {"europe": "Q46", "france": "Q142"}.get(word),
        _phrase_qids=lambda phrases: {},
        _best_world_entity=lambda words: None,
    )
    deposits = [{"table": "deposits", "name": "bank", "affinity": "TEXT", "values": ["UBS", "Barclays"]},
                {"table": "deposits", "name": "deposits", "affinity": "INTEGER", "values": [900, 500]}]
    ranked_banks = 'SELECT "deposits"."bank" FROM "deposits" ORDER BY "deposits"."deposits" DESC LIMIT 1'

    def dropped(question, schema, sql):
        return KnowledgeQuery._uncovered(adapter, question, schema, sql)

    with patch.object(knowledge_query, "closed_class_words", wraps=knowledge_query.closed_class_words):
        assert dropped("Which country has the most deposits?", deposits, ranked_banks) == ["country"]
        assert dropped("total deposits by country", deposits,
                       'SELECT SUM("deposits"."deposits") FROM "deposits"') == ["country"]
        assert dropped("Which bank has the most deposits in Europe?", deposits, ranked_banks) == ["europe"]
        # Realized: a world join brings the country, and the continent filter holds Europe's QID.
        assert dropped("Which country has the most deposits?", deposits,
                       'SELECT "bank__country" AS "country" FROM "query_total" ORDER BY "sum" DESC LIMIT 1') == []
        assert dropped("Which bank has the most deposits in Europe?", deposits,
                       ranked_banks.replace(" ORDER", " WHERE \"country__continent\" = 'Q46' ORDER")) == []
        # A column of that name answers it, and a type noun that only names the rows is not asked for.
        orders = [{"table": "orders", "name": "city", "affinity": "TEXT", "values": ["Paris", "Lyon"]},
                  {"table": "orders", "name": "amount", "affinity": "INTEGER", "values": [12, 14]}]
        assert dropped("Which city has the most orders?", orders,
                       'SELECT "orders"."city" FROM "orders" GROUP BY "orders"."city" '
                       'ORDER BY COUNT(*) DESC LIMIT 1') == []
        assert dropped("total amount for cities in France", orders,
                       'SELECT SUM("amount") FROM "query_filtered" WHERE "city__country" = \'Q142\'') == []


def test_resolved_secondary_relationship_returns_real_knowledgebase_objects():
    from engine.deterministic.plan import JunctionValue
    from engine.deterministic.runtime import (
        assert_equivalent,
        execute_python,
        execute_sql_views,
        materialized_python_views,
    )

    plan = _plan()
    bridge = TableSpec(
        "resolved cities",
        "ResolvedCity",
        "resolved_city",
        "conversation",
        (
            ColumnSpec("cell", "cell", SQLType.TEXT, True, False),
            ColumnSpec("qid", "qid", SQLType.TEXT),
            ColumnSpec("column", "column", SQLType.TEXT),
        ),
    )
    eq = lambda a, b: PredicateValue(a, "=", b)
    relationship = RelationshipSpec(
        "city",
        "city",
        ("city",),
        ("qid",),
        condition=JunctionValue(
            "AND",
            (
                eq(ColumnValue("orders", "city"), ColumnValue(bridge.name, "cell")),
                eq(ColumnValue(bridge.name, "column"), LiteralValue("city")),
            ),
        ),
        secondary=bridge.name,
        secondary_condition=eq(
            ColumnValue(bridge.name, "qid"), ColumnValue("city", "qid")
        ),
    )
    orders = replace(
        plan.table("orders"),
        relationships=(plan.table("orders").relationships[0], relationship),
    )
    plan = replace(plan, tables=(orders, *plan.tables[1:], bridge))
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        for schema in ("conversation", "knowledgebase"):
            connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {schema}")
        for sql in (
            "CREATE TABLE conversation.orders(order_id INTEGER, customer_id INTEGER, amount NUMERIC, city TEXT)",
            "CREATE TABLE conversation.customers(customer_id INTEGER, name TEXT)",
            'CREATE TABLE conversation."resolved cities"(cell TEXT, qid TEXT, "column" TEXT)',
            "CREATE TABLE knowledgebase.city(qid TEXT, name TEXT, country TEXT)",
            "CREATE TABLE knowledgebase.country(qid TEXT, name TEXT)",
            "INSERT INTO conversation.orders VALUES (1, 1, 10, 'Paris'), (2, 1, 20, 'missing')",
            "INSERT INTO conversation.customers VALUES (1, 'Alice')",
            """INSERT INTO conversation."resolved cities" VALUES ('Paris', 'Q90', 'city')""",
            "INSERT INTO knowledgebase.city VALUES ('Q90', 'Paris', 'Q142')",
            "INSERT INTO knowledgebase.country VALUES ('Q142', 'France')",
        ):
            connection.exec_driver_sql(sql)
        package = PythonEmitter().emit(plan)
        result = execute_python(
            package, connection, schema_map={"conversation": "conversation"}
        )
        assert result.views[0].rows[0].orders.city.name == "Paris"
        assert result.views[0].rows[0].orders.city.country.name == "France"
        for python_rows, sql_rows in zip(
            materialized_python_views(result, plan),
            execute_sql_views(SQLEmitter().emit(plan), connection),
            strict=True,
        ):
            assert_equivalent(python_rows, sql_rows)
        assert "secondary=lambda: Base.metadata.tables" in package.files["orders.py"]


def test_correlated_operators_sort_and_outer_join_are_readable_and_equivalent():
    from engine.deterministic.plan import SortedView, SortValue, WindowView
    from engine.deterministic.runtime import (
        assert_equivalent,
        execute_python,
        execute_sql_views,
        materialized_python_views,
    )

    observation = TableSpec(
        "observation",
        "Observation",
        "observation",
        "conversation",
        (
            ColumnSpec("id", "id", SQLType.INTEGER, True, False),
            ColumnSpec("year", "year", SQLType.INTEGER),
            ColumnSpec("amount", "amount", SQLType.INTEGER),
        ),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS conversation")
        connection.exec_driver_sql(
            "CREATE TABLE conversation.observation(id INTEGER, year INTEGER, amount INTEGER)"
        )
        connection.exec_driver_sql(
            "INSERT INTO conversation.observation VALUES (1,2022,10), (2,2023,20), (3,2025,10)"
        )
        for operation, expected in (
            ("share", [0.5, 0.25, 0.25]),
            ("running", [40, 30, 10]),
            ("yoy", [1]),
        ):
            base = ProjectedView(
                "metric_base",
                "metric_combined",
                (
                    SelectedValue("year", ColumnValue("observation", "year")),
                    SelectedValue("amount", ColumnValue("observation", "amount")),
                ),
            )
            plan = AnalysisPlan(
                "metric",
                (observation,),
                (
                    CombinedView("metric_combined", ("observation",)),
                    base,
                    WindowView(
                        "metric_window",
                        base.name,
                        operation,
                        "metric",
                        ViewValue("amount"),
                        time=ViewValue("year") if operation != "share" else None,
                    ),
                    SortedView(
                        "metric_sorted",
                        "metric_window",
                        (
                            SortValue(ViewValue("metric"), True),
                            SortValue(ViewValue("year")),
                        ),
                    ),
                    ProjectedView(
                        "metric_result",
                        "metric_sorted",
                        (SelectedValue("metric", ViewValue("metric")),),
                    ),
                ),
            )
            package = PythonEmitter().emit(plan)
            result = execute_python(
                package, connection, schema_map={"conversation": "conversation"}
            )
            python_views = materialized_python_views(result, plan)
            sql_views = execute_sql_views(SQLEmitter().emit(plan), connection)
            for python_rows, sql_rows in zip(python_views, sql_views, strict=True):
                assert_equivalent(python_rows, sql_rows)
            assert [float(row["metric"]) for row in python_views[-1]] == expected
            assert [float(row["metric"]) for row in sql_views[-1]] == expected
            assert (
                "metric_window = metric_base.for_each("
                in package.files[package.entrypoint]
            )


def test_composition_lowers_selected_bindings_and_executes_real_reference_relationships():
    from unittest.mock import patch

    from engine.compose import ComposeEngine
    from engine.deterministic.compose import lower_composition

    tables = [
        {
            "name": "orders",
            "columns": ["id", "city", "amount"],
            "rows": [[1, "Paris", 100], [2, "Lyon", 80], [3, "Chennai", 150]],
        }
    ]
    world = {
        "name": "knowledgebase facts",
        "columns": ["city", "country"],
        "rows": [["Paris", "France"], ["Lyon", "France"], ["Chennai", "India"]],
        "graph": {
            "edges": [
                {
                    "left_table": "orders",
                    "left_col": "city",
                    "right_table": "Cities",
                    "right_col": "qid",
                }
            ],
            "keys": {"Cities": ("qid",)},
            "columns": {"country": ("Cities", "country")},
            "bridge": "resolved cities",
        },
    }
    schema = [
        {
            "table": "orders",
            "name": column,
            "affinity": "TEXT" if column == "city" else "INTEGER",
            "values": [row[index] for row in tables[0]["rows"]],
        }
        for index, column in enumerate(tables[0]["columns"])
    ]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        for namespace in ("conversation", "knowledgebase"):
            connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {namespace}")
        for statement in (
            "CREATE TABLE conversation.orders(id INTEGER, city TEXT, amount INTEGER)",
            "INSERT INTO conversation.orders VALUES(1,'Paris',100),(2,'Lyon',80),(3,'Chennai',150)",
            'CREATE TABLE conversation."resolved cities"("column" TEXT, value TEXT, world_key TEXT)',
            """INSERT INTO conversation."resolved cities" VALUES('city','Paris','Q90'),('city','Lyon','Q456'),('city','Chennai','Q1352')""",
            'CREATE TABLE knowledgebase."Cities"(qid TEXT, country TEXT)',
            """INSERT INTO knowledgebase."Cities" VALUES('Q90','France'),('Q456','France'),('Q1352','India')""",
        ):
            connection.exec_driver_sql(statement)
        for question, expected in (
            ("total amount by country", [["France", 180], ["India", 150]]),
            ("top 1 country by total amount", [["France", 180]]),
            ("total amount by country over 160", [["France", 180]]),
        ):
            candidate = ComposeEngine().run(tables, question, world=world)
            with patch(
                "engine.deterministic.compose.reference_schema",
                return_value={
                    "Cities": [("qid", SQLType.TEXT, False), ("country", SQLType.TEXT, False)]
                },
            ):
                plan = lower_composition(
                    "metric", tables, schema, candidate["bindings"], world, connection
                )
            result = DeterministicAnalysis(
                plan, conversation_schema="conversation"
            ).run(connection, mode="verify", estimated_rows=3)
            assert sorted([list(row.values()) for row in result.rows]) == expected, (
                question,
                result.rows,
            )


def test_a_world_listing_ends_at_the_filtered_sheet_in_both_emitters():
    # 'amount in France' has no aggregate: the kept rows are the answer, so the trail is the lookup
    # and the filter, and the filtered sheet is the output. The lowering used to refuse it ("world
    # projection requires typed projection bindings"), which left such listings to the semantic search.
    from engine.deterministic.lower import UnsupportedDeterministicPlan
    from engine.deterministic.world import lower_world_query

    rows = [[1, "Paris", 100], [2, "Lyon", 80], [3, "Chennai", 150]]
    schema = [
        {
            "table": "orders",
            "name": column,
            "affinity": "TEXT" if column == "city" else "INTEGER",
            "values": [row[index] for row in rows],
        }
        for index, column in enumerate(("id", "city", "amount"))
    ]
    slots = dict(
        slug="amount_in_france", schema=schema, uploaded=["orders"], foreign_keys=[],
        joins=[{"left_table": "orders", "left_col": "city", "right_table": "city", "right_col": "qid"}],
        bridge_name="orders connected to knowledgebase", route_table="orders", route_column="city",
        meaning_filter={"filter_table": "city", "attr": "country", "value": "Q142"}, own_filters=[],
        world_rate=None, as_of=None, aggregate=None, calculation=None, conversion=None,
        reference_columns={"city": [("qid", SQLType.TEXT, False), ("country", SQLType.TEXT, False)]},
    )
    plan = lower_world_query(**slots)
    assert [view.name for view in plan.views] == [
        "amount_in_france_combined", "amount_in_france_enriched_1", "amount_in_france_filtered",
    ]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        for namespace in ("conversation", "knowledgebase"):
            connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {namespace}")
        for statement in (
            "CREATE TABLE conversation.orders(id INTEGER, city TEXT, amount INTEGER)",
            "INSERT INTO conversation.orders VALUES(1,'Paris',100),(2,'Lyon',80),(3,'Chennai',150)",
            'CREATE TABLE conversation."orders connected to knowledgebase"'
            '("column" TEXT, value TEXT, entity_qid TEXT)',
            """INSERT INTO conversation."orders connected to knowledgebase" VALUES"""
            """('city','Paris','Q90'),('city','Lyon','Q456'),('city','Chennai','Q1352')""",
            "CREATE TABLE knowledgebase.city(qid TEXT, country TEXT)",
            "INSERT INTO knowledgebase.city VALUES('Q90','Q142'),('Q456','Q142'),('Q1352','Q668')",
        ):
            connection.exec_driver_sql(statement)
        result = DeterministicAnalysis(plan, conversation_schema="conversation").run(
            connection, mode="verify", estimated_rows=3
        )
    assert sorted(
        (row["orders__id"], row["orders__amount"], row["city__country"]) for row in result.rows
    ) == [(1, 100, "Q142"), (2, 80, "Q142")], result.rows

    # A currency conversion still needs an aggregate that says what it converts.
    try:
        lower_world_query(**{**slots, "conversion": ("exchange_rate", "rate_to_usd")})
    except UnsupportedDeterministicPlan:
        pass
    else:
        raise AssertionError("a conversion without an aggregate must not lower to a listing")


def test_a_one_sheet_trail_starts_at_the_reference_lookup():
    # docs/SHEETS_AS_REASONING.md rule 3: 'combined' is a stage only for a real join of two or more
    # uploaded sheets. The one-sheet 'total sales in france' trail opened with "Combined into one table
    # · from sales" (2026-09-27). Neither emitter materializes a one-sheet entry: the lookup reads the
    # upload itself, so its SQL and Python name no relation the trail does not show (rule 2).
    from engine.deterministic.world import lower_world_query

    rows = [[1, "Paris", 100], [2, "Lyon", 80], [3, "Chennai", 150]]
    plan = lower_world_query(
        slug="orders_in_france",
        schema=[{"table": "orders", "name": column, "affinity": "TEXT" if column == "city" else "INTEGER",
                 "values": [row[index] for row in rows]}
                for index, column in enumerate(("id", "city", "amount"))],
        uploaded=["orders"], foreign_keys=[],
        joins=[{"left_table": "orders", "left_col": "city", "right_table": "city", "right_col": "qid"}],
        bridge_name="orders connected to knowledgebase", route_table="orders", route_column="city",
        meaning_filter={"filter_table": "city", "attr": "country", "value": "Q142"}, own_filters=[],
        world_rate=None, as_of=None, aggregate=None, calculation=None, conversion=None,
        reference_columns={"city": [("qid", SQLType.TEXT, False), ("country", SQLType.TEXT, False)]},
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        for namespace in ("conversation", "knowledgebase"):
            connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {namespace}")
        for statement in (
            "CREATE TABLE conversation.orders(id INTEGER, city TEXT, amount INTEGER)",
            "INSERT INTO conversation.orders VALUES(1,'Paris',100),(2,'Lyon',80),(3,'Chennai',150)",
            'CREATE TABLE conversation."orders connected to knowledgebase"'
            '("column" TEXT, value TEXT, entity_qid TEXT)',
            """INSERT INTO conversation."orders connected to knowledgebase" VALUES"""
            """('city','Paris','Q90'),('city','Lyon','Q456'),('city','Chennai','Q1352')""",
            "CREATE TABLE knowledgebase.city(qid TEXT, country TEXT)",
            "INSERT INTO knowledgebase.city VALUES('Q90','Q142'),('Q456','Q142'),('Q1352','Q668')",
        ):
            connection.exec_driver_sql(statement)
        result = DeterministicAnalysis(plan, conversation_schema="conversation").run(
            connection, mode="verify", estimated_rows=3
        )
    assert result.mode.value == "verify"                                  # both programs ran and agreed
    stages = ["orders_in_france_enriched_1", "orders_in_france_filtered"]
    assert result.emission.sql.manifest["views"] == result.emission.python.manifest["views"] == stages
    assert not any("orders_in_france_combined AS" in " ".join(statement.split()[:4])
                   for statement in result.emission.sql.statements)      # no combined view is created
    shown = result.record()["views"]
    assert [view["name"] for view in shown] == stages
    lookup = shown[0]
    assert lookup["inputs"] == ["orders"], lookup["inputs"]
    # The lookup's own programs read the upload: an inline SELECT in SQL, the ORM rows in Python.
    assert 'FROM (SELECT "conversation"."orders"."id" AS "orders__id"' in lookup["sql"], lookup["sql"]
    assert 'FROM "orders_in_france_combined"' not in lookup["sql"]
    assert "select(Order)" in lookup["python"] and "# View: orders_in_france_combined" not in lookup["python"]
    assert [len(view["rows"]) for view in shown] == [3, 2]

    # Two uploaded sheets really joined keep their combined sheet, and the next sheet reads from it.
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        for namespace in ("conversation", "knowledgebase"):
            connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {namespace}")
        for statement in (
            "CREATE TABLE conversation.customers (customer_id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
            "CREATE TABLE conversation.orders (order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL, "
            "amount NUMERIC NOT NULL, city TEXT NOT NULL)",
            "CREATE TABLE knowledgebase.country (qid TEXT PRIMARY KEY, name TEXT NOT NULL)",
            "CREATE TABLE knowledgebase.city (qid TEXT PRIMARY KEY, name TEXT NOT NULL, country TEXT NOT NULL)",
            "INSERT INTO conversation.customers VALUES (1, 'Ada'), (2, 'Lin')",
            "INSERT INTO knowledgebase.country VALUES ('Q142', 'France'), ('Q30', 'United States')",
            "INSERT INTO knowledgebase.city VALUES ('Q90', 'Paris', 'Q142'), ('Q60', 'New York City', 'Q30')",
            "INSERT INTO conversation.orders VALUES (10, 1, 12.50, 'Q90'), (11, 1, 7.50, 'Q90'), (12, 2, 100, 'Q60')",
        ):
            connection.exec_driver_sql(statement)
        joined = DeterministicAnalysis(_plan(), conversation_schema="conversation").run(
            connection, mode="verify", estimated_rows=3
        ).record()["views"]
    assert joined[0]["name"] == "total_amount_combined", [view["name"] for view in joined]
    assert joined[1]["inputs"] == ["total_amount_combined"]


def test_a_world_trail_shows_entity_labels_and_runs_the_stored_qids():
    # docs/SHEETS_AS_REASONING.md rule 5: the lookup and filtered sheets of 'everything in France' showed
    # Q142 and Q90 (2026-09-27). Displayed rows show labels; both programs keep the stored literal, so the
    # parity check still compares what ran. An uploaded 'Q1' (a quarter) is not an entity: only columns
    # the plan traces to a knowledgebase table are labelled.
    from engine.deterministic.world import lower_world_query

    rows = [[1, "Paris", "Q1", 100], [2, "Lyon", "Q2", 80], [3, "Chennai", "Q1", 150]]
    names = {"Q90": "Paris", "Q456": "Lyon", "Q1352": "Chennai", "Q142": "France", "Q668": "India",
             "Q1": "universe"}
    asked = []

    def labels(entities):
        asked.append(set(entities))
        return {qid: names[qid] for qid in entities if qid in names}

    def run(aggregate, own_filters=()):
        plan = lower_world_query(
            slug="orders_in_france",
            schema=[{"table": "orders", "name": column,
                     "affinity": "INTEGER" if column in ("id", "amount") else "TEXT",
                     "values": [row[index] for row in rows]}
                    for index, column in enumerate(("id", "city", "quarter", "amount"))],
            uploaded=["orders"], foreign_keys=[],
            joins=[{"left_table": "orders", "left_col": "city", "right_table": "city", "right_col": "qid"}],
            bridge_name="orders connected to knowledgebase", route_table="orders", route_column="city",
            meaning_filter={"filter_table": "city", "attr": "country", "value": "Q142"},
            own_filters=list(own_filters),
            world_rate=None, as_of=None, aggregate=aggregate, calculation=None, conversion=None,
            reference_columns={"city": [("qid", SQLType.TEXT, False), ("country", SQLType.TEXT, False)]},
        )
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            for namespace in ("conversation", "knowledgebase"):
                connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {namespace}")
            for statement in (
                "CREATE TABLE conversation.orders(id INTEGER, city TEXT, quarter TEXT, amount INTEGER)",
                "INSERT INTO conversation.orders VALUES(1,'Paris','Q1',100),(2,'Lyon','Q2',80),"
                "(3,'Chennai','Q1',150)",
                'CREATE TABLE conversation."orders connected to knowledgebase"'
                '("column" TEXT, value TEXT, entity_qid TEXT)',
                """INSERT INTO conversation."orders connected to knowledgebase" VALUES"""
                """('city','Paris','Q90'),('city','Lyon','Q456'),('city','Chennai','Q1352')""",
                "CREATE TABLE knowledgebase.city(qid TEXT, country TEXT)",
                "INSERT INTO knowledgebase.city VALUES('Q90','Q142'),('Q456','Q142'),('Q1352','Q668')",
            ):
                connection.exec_driver_sql(statement)
            return DeterministicAnalysis(plan, conversation_schema="conversation", labels=labels).run(
                connection, mode="verify", estimated_rows=3
            )

    listing = run(None)
    assert listing.mode.value == "verify"                       # both programs ran and agreed on the QIDs
    views = {view["logical_name"]: view for view in listing.record()["views"]}
    lookup, kept = views["enriched_1"], views["filtered"]

    def column(view, name):
        return [row[view["columns"].index(name)] for row in view["rows"]]

    assert column(lookup, "city__qid") == ["Paris", "Lyon", "Chennai"], lookup["rows"]
    assert column(lookup, "city__country") == ["France", "France", "India"], lookup["rows"]
    assert column(kept, "city__country") == ["France", "France"], kept["rows"]
    assert column(lookup, "orders__quarter") == ["Q1", "Q2", "Q1"]      # an uploaded value is never an entity
    assert column(kept, "orders__city") == ["Paris", "Lyon"]
    assert asked == [{"Q90", "Q456", "Q1352", "Q142", "Q668"}], asked   # one lookup, reference columns only
    assert "'Q142'" in kept["sql"] and "Q142" in kept["python"]          # the executed programs keep the literal
    assert [row["city__country"] for row in listing.rows] == ["Q142", "Q142"]
    assert [row["city__country"] for row in listing.output_rows()] == ["France", "France"]
    assert [row["orders__quarter"] for row in listing.output_rows()] == ["Q1", "Q2"]

    total = run(("SUM", "orders", "amount"))
    views = {view["logical_name"]: view for view in total.record()["views"]}
    assert column(views["filtered"], "city__country") == ["France", "France"]
    assert views["total"]["rows"] == [[180]] and [tuple(row.values()) for row in total.output_rows()] == [(180,)]

    # The filter sheet's label names its condition in labels (step 4, rule 5); the program compared Q142. An
    # uploaded 'Q1' in the same condition stays 'Q1', though the resolver would call it 'universe'.
    assert views["filtered"]["label"] == "where country = 'France'", views["filtered"]["label"]
    quarter = run(("SUM", "orders", "amount"), own_filters=[("orders", "quarter", "Q1")])
    kept = {view["logical_name"]: view for view in quarter.record()["views"]}["filtered"]
    assert kept["label"] == "where country = 'France' and quarter = 'Q1'", kept["label"]
    assert kept["rows"] == [[1, "Paris", "Q1", 100, "Paris", "France"]], kept["rows"]


def test_world_projections_group_the_kept_rows_by_the_named_world_attribute():
    # 'which continent has the highest total amount' failed in production with "world projection requires
    # typed projection bindings" (2026-09-27): the served path refused every question that names a world
    # column. Each shape now lowers to the own-data and compose grammar, a grouped total, and runs in both
    # programs with verify parity; the grouped QIDs display as labels.
    from engine.deterministic.lower import UnsupportedDeterministicPlan
    from engine.deterministic.world import lower_world_query

    names = {"Q46": "Europe", "Q48": "Asia", "Q49": "North America", "Q18": "South America"}

    def labels(entities):
        return {qid: names[qid] for qid in entities if qid in names}

    countries = {"France": ("Q142", "Q46"), "Germany": ("Q183", "Q46"), "China": ("Q148", "Q48"),
                 "India": ("Q668", "Q48"), "United States": ("Q30", "Q49"), "Brazil": ("Q155", "Q18"),
                 "Japan": ("Q17", "Q48"), "Atlantis": ("Q999", None)}

    def run(sales, **binding):
        rows = [(index + 1, country, amount) for index, (country, amount) in enumerate(sales)]
        binding.setdefault("dimension", ("country", "continent"))
        plan = lower_world_query(
            slug="continents",
            schema=[{"table": "sales", "name": column, "affinity": "TEXT" if column == "country" else "INTEGER",
                     "values": [row[index] for row in rows]}
                    for index, column in enumerate(("id", "country", "amount"))],
            uploaded=["sales"], foreign_keys=[],
            joins=[{"left_table": "sales", "left_col": "country", "right_table": "country", "right_col": "qid"}],
            bridge_name="sales connected to knowledgebase", route_table="sales", route_column="country",
            meaning_filter=None, own_filters=[], world_rate=None, as_of=None, calculation=None, conversion=None,
            reference_columns={"country": [("qid", SQLType.TEXT, False), ("continent", SQLType.TEXT, False)]},
            **binding,
        )
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            for namespace in ("conversation", "knowledgebase"):
                connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {namespace}")
            connection.exec_driver_sql("CREATE TABLE conversation.sales(id INTEGER, country TEXT, amount INTEGER)")
            connection.exec_driver_sql(
                'CREATE TABLE conversation."sales connected to knowledgebase"'
                '("column" TEXT, value TEXT, entity_qid TEXT)')
            connection.exec_driver_sql("CREATE TABLE knowledgebase.country(qid TEXT, continent TEXT)")
            for row in rows:
                connection.exec_driver_sql("INSERT INTO conversation.sales VALUES (?, ?, ?)", row)
            for country, (qid, continent) in countries.items():
                connection.exec_driver_sql(
                    'INSERT INTO conversation."sales connected to knowledgebase" VALUES (?, ?, ?)',
                    ("country", country, qid))
                connection.exec_driver_sql("INSERT INTO knowledgebase.country VALUES (?, ?)", (qid, continent))
            result = DeterministicAnalysis(plan, conversation_schema="conversation", labels=labels).run(
                connection, mode="verify", estimated_rows=len(rows))
        assert result.mode.value == "verify"                  # both programs ran and agreed on the QIDs
        return plan, result

    sales = [("France", 120), ("Germany", 80), ("China", 200), ("India", 50), ("United States", 300),
             ("Brazil", 90), ("Japan", 60)]
    highest = ("SUM", "sales", "amount")
    plan, result = run(sales, aggregate=highest, order="DESC")
    assert [view.name for view in plan.stages()] == [
        "continents_enriched_1", "continents_total", "continents_top_results"]
    assert [tuple(row.values()) for row in result.output_rows()] == [("Asia", 310)]
    assert [tuple(row.values()) for row in result.rows] == [("Q48", 310)]        # the programs keep the QID
    record = {view["logical_name"]: view for view in result.record()["views"]}
    assert record["total"]["op"] == "group_agg" and record["top_results"]["op"] == "topn"
    assert sorted(record["total"]["rows"]) == [["Asia", 310], ["Europe", 200], ["North America", 300],
                                               ["South America", 90]]
    _plan_asc, lowest = run(sales, aggregate=highest, order="ASC")
    assert [tuple(row.values()) for row in lowest.output_rows()] == [("South America", 90)]
    # A tie is broken by the stored key in both programs: Asia (Q48) before North America (Q49).
    _plan_tie, tie = run([*sales[:4], ("United States", 310), *sales[5:]], aggregate=highest, order="DESC")
    assert [tuple(row.values()) for row in tie.output_rows()] == [("Asia", 310)]

    # 'which continent …' without an aggregate: each value and the rows that hold it.
    _plan_values, values = run(sales, aggregate=None)
    assert sorted(tuple(row.values()) for row in values.output_rows()) == [
        ("Asia", 3), ("Europe", 2), ("North America", 1), ("South America", 1)]
    # 'how many continents': distinct values; a country with no continent is not one.
    plan, counted = run([*sales, ("Atlantis", 5)], aggregate=("COUNT", "sales", None))
    assert [view.name for view in plan.stages()][-2:] == ["continents_groups", "continents_total"]
    assert [tuple(row.values()) for row in counted.output_rows()] == [(4,)]

    # Negative: a ranking needs the attribute it ranks, and a SUM or AVG to rank by.
    for binding, message in (({"aggregate": highest, "order": "DESC", "dimension": None}, "attribute"),
                             ({"aggregate": ("COUNT", "sales", None), "order": "DESC"}, "SUM or AVG"),
                             ({"aggregate": None, "order": "ASC"}, "SUM or AVG")):
        try:
            run(sales, **binding)
        except UnsupportedDeterministicPlan as exc:
            assert message in str(exc), exc
        else:
            raise AssertionError(f"lowered an unsupported projection: {binding}")



def test_a_converted_total_is_grouped_or_ranked_by_the_column_the_question_names():
    # Chrome exploration (2026-10-01): "which city has the highest total amount in US dollars" was answered with
    # the total of every city, and "total amount by country in US dollars" counted the orders per country. A
    # converted SUM now lowers per an uploaded column or a world attribute, ranked or not, and both programs
    # agree.
    from engine.deterministic.world import lower_world_query

    rows = [(1, "Paris", "EUR", 100), (2, "Lyon", "EUR", 80), (3, "London", "GBP", 50), (4, "Austin", "USD", 120)]
    cities = {"Paris": ("Q90", "Q142"), "Lyon": ("Q456", "Q142"), "London": ("Q84", "Q145"),
              "Austin": ("Q16559", "Q30")}
    names = {"Q142": "France", "Q145": "United Kingdom", "Q30": "United States"}
    rates = {"EUR": "1.25", "GBP": "2", "USD": "1"}

    def run(**binding):
        plan = lower_world_query(
            slug="totals",
            schema=[{"table": "orders", "name": column,
                     "affinity": "INTEGER" if column in ("id", "amount") else "TEXT",
                     "values": [row[index] for row in rows]}
                    for index, column in enumerate(("id", "city", "currency", "amount"))],
            uploaded=["orders"], foreign_keys=[],
            joins=[{"left_table": "orders", "left_col": "city", "right_table": "city", "right_col": "qid"}],
            bridge_name="orders connected to knowledgebase", route_table="orders", route_column="city",
            meaning_filter=None, own_filters=[],
            world_rate={"fact": "orders", "ccy_col": "currency", "date_col": None, "rate_col": "rate_to_usd"},
            as_of="2026-10-01", aggregate=("SUM", "orders", "amount"), calculation=None, conversion=None,
            reference_columns={
                "city": [("qid", SQLType.TEXT, False), ("country", SQLType.TEXT, False)],
                "exchange_rate": [("currency_code", SQLType.TEXT, False), ("date", SQLType.TEXT, False),
                                  ("rate_to_usd", SQLType.REAL, False)],
            },
            **binding,
        )
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            for namespace in ("conversation", "knowledgebase"):
                connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {namespace}")
            connection.exec_driver_sql(
                "CREATE TABLE conversation.orders(id INTEGER, city TEXT, currency TEXT, amount INTEGER)")
            connection.exec_driver_sql(
                'CREATE TABLE conversation."orders connected to knowledgebase"'
                '("column" TEXT, value TEXT, entity_qid TEXT)')
            connection.exec_driver_sql("CREATE TABLE knowledgebase.city(qid TEXT, country TEXT)")
            connection.exec_driver_sql(
                "CREATE TABLE knowledgebase.exchange_rate(currency_code TEXT, date TEXT, rate_to_usd REAL)")
            for row in rows:
                connection.exec_driver_sql("INSERT INTO conversation.orders VALUES (?, ?, ?, ?)", row)
            for city, (qid, country) in cities.items():
                connection.exec_driver_sql(
                    'INSERT INTO conversation."orders connected to knowledgebase" VALUES (?, ?, ?)',
                    ("city", city, qid))
                connection.exec_driver_sql("INSERT INTO knowledgebase.city VALUES (?, ?)", (qid, country))
            for code, rate in rates.items():
                connection.exec_driver_sql(
                    "INSERT INTO knowledgebase.exchange_rate VALUES (?, ?, ?)", (code, "2026-10-01", rate))
            result = DeterministicAnalysis(
                plan, conversation_schema="conversation",
                labels=lambda entities: {qid: names[qid] for qid in entities if qid in names},
            ).run(connection, mode="verify", estimated_rows=len(rows))
        assert result.mode.value == "verify"                  # both programs ran and agreed
        return [tuple(row.values()) for row in result.output_rows()]

    # Converted: Paris 125, Lyon 100, London 100, Austin 120; France 225.
    assert run(dimension=("orders", "city"), order="DESC") == [("Paris", 125)]
    assert sorted(run(dimension=("orders", "city"))) == [
        ("Austin", 120), ("London", 100), ("Lyon", 100), ("Paris", 125)]
    assert sorted(run(dimension=("city", "country"))) == [
        ("France", 225), ("United Kingdom", 100), ("United States", 120)]
    assert run(dimension=("city", "country"), order="ASC") == [("United Kingdom", 100)]


def test_the_column_a_total_is_grouped_or_ranked_by_is_read_from_the_question():
    from engine.knowledge_tables import KnowledgeTableQuery

    schema = [{"table": "orders", "name": name, "affinity": affinity}
              for name, affinity in (("order ID", "INTEGER"), ("customer", "TEXT"), ("city", "TEXT"),
                                     ("tier", "TEXT"), ("currency", "TEXT"), ("amount", "INTEGER"))]

    def dimension(question):
        return KnowledgeTableQuery._own_dimension(question, schema)

    assert dimension("which city has the highest total amount in US dollars?") == ("orders", "city", "DESC")
    assert dimension("Which tier has the lowest total amount in euros?") == ("orders", "tier", "ASC")
    assert dimension("total amount by tier in US dollars") == ("orders", "tier", None)
    assert dimension("total amount by cities in US dollars") == ("orders", "city", None)
    assert dimension("total amount for each customer in GBP") == ("orders", "customer", None)
    # A value is not a column, a key or number column is not a dimension, and "which" without a ranking only
    # asks which value, so none of these is grouped.
    assert dimension("total amount ordered by Sherlock Holmes in US dollars") is None
    assert dimension("average amount per order ID") is None
    assert dimension("total amount in US dollars") is None
    assert dimension("which city is London in") is None
    # A saved reference sheet with the same column: the measure's own sheet answers, and without a measure sheet
    # the two stay ambiguous.
    referenced = schema + [{"table": "tier", "name": "tier", "affinity": "TEXT"},
                           {"table": "tier", "name": "benefits", "affinity": "TEXT"}]
    assert KnowledgeTableQuery._own_dimension("total amount by tier in US dollars", referenced, "orders") == (
        "orders", "tier", None)
    assert KnowledgeTableQuery._own_dimension("total amount by tier in US dollars", referenced) is None


def test_sum_or_avg_over_a_text_column_is_refused():
    # 'What is the total population?' summed knowledgebase."city".population, which is stored as text: the Python
    # program concatenated the strings into 1426479827518622326844519127886040 and served it, and the SQL
    # program cannot sum text at all (2026-09-28). Such a plan has no one meaning, so it is refused before
    # either program exists. MIN and MAX of text are an ordering, not arithmetic, and stay.
    table = TableSpec(
        "cities",
        "City",
        "cities",
        "conversation",
        (
            ColumnSpec("id", "id", SQLType.INTEGER, primary_key=True, nullable=False),
            ColumnSpec("name", "name", SQLType.TEXT),
            ColumnSpec("population", "population", SQLType.TEXT),
            ColumnSpec("sales", "sales", SQLType.INTEGER),
        ),
    )

    def plan(function, column, carried=False):
        views = [CombinedView("cities_combined", ("cities",))]
        operand = ColumnValue("cities", column)
        if carried:                                   # the column crosses a projection first
            views.append(
                ProjectedView("cities_base", "cities_combined", (SelectedValue(column, operand),))
            )
            operand = ViewValue(column)
        views.append(
            ReducedView("cities_total", views[-1].name, (AggregateValue("total", function, operand),))
        )
        return AnalysisPlan("cities", (table,), tuple(views))

    for function in ("SUM", "AVG"):
        for carried in (False, True):
            try:
                plan(function, "population", carried)
            except ValueError as exc:
                assert "stored as text" in str(exc), exc
            else:
                raise AssertionError(f"{function} over a text column was accepted (carried={carried})")
    plan("SUM", "sales")
    plan("AVG", "sales", carried=True)
    plan("MAX", "population")
    plan("MIN", "name", carried=True)


def test_a_binary_float_reference_column_is_read_as_numeric_by_both_programs():
    # 'average atomic mass' reads knowledgebase."Elements in the World".mass, a double precision column keyed
    # by name (2026-09-27). The ORM read 1.008 as 1.00800000000000000711 while SQL read 1.008, so verify
    # failed at the lookup; and Postgres sums floats with rounding a decimal sum does not have. Both programs
    # now read the column as NUMERIC. (SQLite computes the SQL program's AVG in floats, so the average is
    # checked against live Postgres in tests.test_world.)
    from engine.deterministic.runtime import VerificationMismatch
    from engine.deterministic.world import lower_world_query

    rows = [(1, "Hydrogen", 2), (2, "Oxygen", 1), (3, "Carbon", 3)]

    def run(aggregate, binary_float=True, mode="verify"):
        plan = lower_world_query(
            slug="mass",
            schema=[{"table": "samples", "name": column, "affinity": "TEXT" if column == "element" else "INTEGER",
                     "values": [row[index] for row in rows]}
                    for index, column in enumerate(("id", "element", "qty"))],
            uploaded=["samples"], foreign_keys=[],
            joins=[{"left_table": "samples", "left_col": "element", "right_table": "elements", "right_col": "name"}],
            bridge_name="samples connected to knowledgebase", route_table="samples", route_column="element",
            meaning_filter=None, own_filters=[], world_rate=None, as_of=None, calculation=None, conversion=None,
            aggregate=aggregate, bridge_key="world_key", reference_keys={"elements": ("name",)},
            reference_columns={"elements": [("name", SQLType.TEXT, False), ("mass", SQLType.REAL, binary_float)]},
        )
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.begin() as connection:
            for namespace in ("conversation", "knowledgebase"):
                connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {namespace}")
            for statement in (
                "CREATE TABLE conversation.samples(id INTEGER, element TEXT, qty INTEGER)",
                "INSERT INTO conversation.samples VALUES (1,'Hydrogen',2),(2,'Oxygen',1),(3,'Carbon',3)",
                'CREATE TABLE conversation."samples connected to knowledgebase"'
                '("column" TEXT, value TEXT, world_key TEXT)',
                """INSERT INTO conversation."samples connected to knowledgebase" VALUES """
                """('element','Hydrogen','Hydrogen'),('element','Oxygen','Oxygen'),('element','Carbon','Carbon')""",
                "CREATE TABLE knowledgebase.elements(name TEXT, mass REAL)",
                "INSERT INTO knowledgebase.elements VALUES ('Hydrogen',1.008),('Oxygen',15.999),('Carbon',12.011)",
            ):
                connection.exec_driver_sql(statement)
            return DeterministicAnalysis(plan, conversation_schema="conversation").run(
                connection, mode=mode, estimated_rows=3)

    total = run(("SUM", "elements", "mass"))
    assert total.mode.value == "verify"                                  # every stage agreed
    # A recorded sheet crosses the wire as the result does (engine.numeric.wire_value): the ORM's Decimal
    # masses become exact JSON scalars, a decimal a float cannot hold exactly as its text.
    import json

    python = run(("SUM", "elements", "mass"), mode="python")
    assert python.mode.value == "python" and any(
        isinstance(value, Decimal) for rows in python.view_rows for row in rows for value in row.values())
    shown = json.loads(json.dumps(python.record()["views"]))
    assert sorted(row[-1] for row in shown[0]["rows"]) == ["1.008", "12.011", "15.999"], shown[0]["rows"]
    assert [Decimal(str(value)) for row in total.rows for value in row.values()] == [Decimal("29.018")]
    lookup = total.record()["views"][0]
    assert 'CAST("knowledgebase"."elements"."mass" AS NUMERIC)' in lookup["sql"], lookup["sql"]
    assert "NumericFloat" in total.emission.python.files["elements.py"]
    # Contrast: read as a plain REAL, the two programs disagree at the lookup, as they did in production.
    try:
        run(("SUM", "elements", "mass"), binary_float=False)
    except VerificationMismatch as exc:
        assert "1.00800000000000000711" in str(exc) and "mass_enriched_1" in "".join(exc.__notes__), exc
    else:
        raise AssertionError("a binary float read as REAL passed verify")


def test_decomposed_dag_crosses_branches_and_excludes_existing_pairs():
    from engine.deterministic.plan import SortedView, SortValue

    customers = TableSpec(
        "customers", "Customer", "customers", "conversation",
        (
            ColumnSpec("id", "id", SQLType.INTEGER, True, False),
            ColumnSpec("customer", "customer", SQLType.TEXT, False, False),
        ),
    )
    products = TableSpec(
        "products", "Product", "products", "conversation",
        (
            ColumnSpec("id", "id", SQLType.INTEGER, True, False),
            ColumnSpec("product", "product", SQLType.TEXT, False, False),
        ),
    )
    history = TableSpec(
        "history", "History", "history", "conversation",
        (
            ColumnSpec("id", "id", SQLType.INTEGER, True, False),
            ColumnSpec("customer", "customer", SQLType.TEXT, False, True),
            ColumnSpec("product", "product", SQLType.TEXT, False, True),
        ),
    )
    views = (
        CombinedView("offers_customers_combined", ("customers",)),
        ProjectedView(
            "offers_customers_result", "offers_customers_combined",
            (SelectedValue("customer", ColumnValue("customers", "customer")),),
        ),
        SortedView(
            "offers_customers_top", "offers_customers_result",
            (SortValue(ViewValue("customer")),), 2,
        ),
        CombinedView("offers_products_combined", ("products",)),
        ProjectedView(
            "offers_products_result", "offers_products_combined",
            (SelectedValue("product", ColumnValue("products", "product")),),
        ),
        SortedView(
            "offers_products_top", "offers_products_result",
            (SortValue(ViewValue("product")),), 2,
        ),
        CombinedView("offers_history_combined", ("history",)),
        ProjectedView(
            "offers_history_result", "offers_history_combined",
            (
                SelectedValue("customer", ColumnValue("history", "customer")),
                SelectedValue("product", ColumnValue("history", "product")),
            ),
        ),
        CrossView(
            "offers_candidates", "offers_customers_top", "offers_products_top"
        ),
        AntiJoinView(
            "offers_result", "offers_candidates", "offers_history_result",
            (MergeKey("customer", "customer"), MergeKey("product", "product")),
            (SortValue(ViewValue("customer")), SortValue(ViewValue("product"))),
        ),
    )
    plan = AnalysisPlan(
        "offers", (customers, products, history), views, "offers_result",
        (
            PlanSection("customers", "Customers", "top customers", tuple(v.name for v in views[:3])),
            PlanSection("products", "Products", "top products", tuple(v.name for v in views[3:6])),
            PlanSection("history", "History", "purchased pairs", tuple(v.name for v in views[6:8])),
            PlanSection("candidates", "Candidates", "candidate pairs", (views[8].name,), ("customers", "products")),
            PlanSection("result", "Not purchased", "not purchased", (views[9].name,), ("candidates", "history")),
        ),
    )
    result = _execute_fixture(
        plan,
        (
            "CREATE TABLE conversation.customers(id INTEGER PRIMARY KEY, customer TEXT NOT NULL)",
            "CREATE TABLE conversation.products(id INTEGER PRIMARY KEY, product TEXT NOT NULL)",
            "CREATE TABLE conversation.history(id INTEGER PRIMARY KEY, customer TEXT, product TEXT)",
            "INSERT INTO conversation.customers VALUES (1, 'A'), (2, 'B')",
            "INSERT INTO conversation.products VALUES (1, 'X'), (2, 'Y')",
            "INSERT INTO conversation.history VALUES (1, 'A', 'X'), (2, NULL, 'Y')",
        ),
        estimated_rows=6,
    )
    assert result.rows == (
        {"customer": "A", "product": "Y"},
        {"customer": "B", "product": "X"},
        {"customer": "B", "product": "Y"},
    )
    record = result.record()
    assert record["manifest"]["output"] == "offers_result"
    assert record["views"][-1]["inputs"] == [
        "offers_candidates", "offers_history_result"
    ]
    assert ".cross(" in record["views"][-2]["python"]
    assert ".anti_join(" in record["views"][-1]["python"]
    assert "NOT EXISTS" in record["views"][-1]["sql"]


def test_merge_inputs_must_be_flat_value_relations():
    from engine.deterministic.plan import SortedView, SortValue

    items = TableSpec(
        "items", "Item", "items", "conversation",
        (
            ColumnSpec("id", "id", SQLType.INTEGER, True, False),
            ColumnSpec("score", "score", SQLType.INTEGER, False, False),
        ),
    )
    try:
        AnalysisPlan(
            "unsafe",
            (items,),
            (
                CombinedView("unsafe_left", ("items",)),
                SortedView(
                    "unsafe_left_top", "unsafe_left",
                    (SortValue(ColumnValue("items", "score"), True),), 2,
                ),
                CombinedView("unsafe_right", ("items",)),
                SortedView(
                    "unsafe_right_top", "unsafe_right",
                    (SortValue(ColumnValue("items", "score"), True),), 2,
                ),
                CrossView("unsafe_pairs", "unsafe_left_top", "unsafe_right_top"),
            ),
        )
        raise AssertionError("an ORM-object relation reached a flattening merge")
    except ValueError as exc:
        assert "projected or reduced" in str(exc)


def test_section_tree_must_match_the_executable_view_dependencies():
    plan = _plan()
    sections = tuple(
        PlanSection(
            f"stage_{index}", f"Stage {index}", f"stage {index}",
            (view.name,), (() if index == 0 else (f"stage_{index - 1}",)),
        )
        for index, view in enumerate(plan.views)
    )
    AnalysisPlan(plan.slug, plan.tables, plan.views, plan.output, sections)
    wrong = sections[:-1] + (replace(sections[-1], inputs=("stage_0",)),)
    try:
        AnalysisPlan(plan.slug, plan.tables, plan.views, plan.output, wrong)
        raise AssertionError("the displayed section tree diverged from executable inputs")
    except ValueError as exc:
        assert "cross-section view dependencies" in str(exc)


# Discover the contract cases so new tests cannot be omitted from the module runner.
def test_a_slug_naming_wrapper_state_or_python_keyword_still_executes():
    """Durable slugs may collide with wrapper state or Python reserved words.

    Both must execute without changing the workbook/SQL slug.
    """
    items = TableSpec(
        name="items",
        class_name="Item",
        attribute="items",
        schema="conversation",
        columns=(
            ColumnSpec(
                "item_id", "item_id", SQLType.INTEGER, primary_key=True, nullable=False
            ),
            ColumnSpec("amount", "amount", SQLType.INTEGER, nullable=False),
        ),
    )
    for slug in ("session", "row_limit", "yield", "class"):
        plan = AnalysisPlan(
            slug=slug,
            tables=(items,),
            views=(
                CombinedView(f"{slug}_combined", ("items",)),
                ReducedView(
                    f"{slug}_total",
                    f"{slug}_combined",
                    (AggregateValue("total", "SUM", ColumnValue("items", "amount")),),
                ),
            ),
        )
        result = _execute_fixture(
            plan,
            [
                (
                    "CREATE TABLE conversation.items ("
                    "item_id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)"
                ),
                "INSERT INTO conversation.items VALUES (1, 10), (2, 32)",
            ],
            mode="verify",
            estimated_rows=2,
        )
        assert result.rows == ({"total": 42},), (slug, result.rows)
        # the stage record carries the Python beside the SQL for the workbook
        views = result.record()["views"]
        assert all(view["python"] for view in views), slug
        package = PythonEmitter().emit(plan)
        expected_method = f"analysis_{slug}" if slug in {"yield", "class"} else slug
        assert package.manifest["slug"] == slug
        assert package.manifest["entrypoint_method"] == expected_method
        assert f"    def {expected_method}(self) -> AnalysisResult:" in package.files[package.entrypoint]


def test_every_stage_reports_the_exact_python_that_produced_it():
    """A stage's displayed Python must be a literal slice of the module that executed,
    never a re-rendering of the plan, or the workbook would show code that never ran."""
    plan = _plan()
    package = PythonEmitter().emit(plan)
    source = package.files[package.entrypoint]
    sources = package.manifest["view_sources"]
    assert set(sources) == {view.name for view in plan.views}
    for view in plan.views:
        segment = sources[view.name]
        assert segment.startswith(f"        # View: {view.name}\n"), view.name
        assert segment in source, view.name
    assert "view_sources" not in package.record()["manifest"]
    assert package.record()["manifest"]["emitter_version"] == 9
    # A one-sheet entry is no stage: the stage that reads it opens with the upload's ORM load, and that
    # slice of the module is what the sheet shows.
    orders = _plan().table("orders")
    one = AnalysisPlan(
        "amount",
        (replace(orders, relationships=()),),
        (
            CombinedView("amount_combined", ("orders",)),
            ReducedView(
                "amount_total",
                "amount_combined",
                (AggregateValue("total", "SUM", ColumnValue("orders", "amount")),),
            ),
        ),
    )
    package = PythonEmitter().emit(one)
    source = package.files[package.entrypoint]
    sources = package.manifest["view_sources"]
    assert set(sources) == {"amount_total"} and package.manifest["views"] == ["amount_total"]
    assert sources["amount_total"].startswith("        # Rows of orders\n")
    assert "        # View: amount_total\n" in sources["amount_total"]
    assert sources["amount_total"] in source
    assert package.manifest["view_inputs"] == {"amount_total": ["orders"]}


def test_streamed_trace_carries_the_same_derivation_as_the_returned_trace():
    """SHEETS_AS_REASONING: the streamed trail and the returned trail are the same trail.

    The compose trace whitelist bounds the wire payload. When it listed `sql` but not
    `python`, a streamed sheet arrived with SQL only, so the workbook badge fell back to
    SQL even though Python had produced the rows.
    """
    from engine.knowledge_compose import _TRACE_VIEW_FIELDS, _trace_view

    assert "python" in _TRACE_VIEW_FIELDS
    view = {
        "name": "total_amount_total",
        "op": "group_agg",
        "label": "total",
        "sql": "SELECT SUM(x) AS total FROM prior",
        "python": "        # View: total_amount_total\n        total = prior.reduce(...)",
        "columns": ["total"],
        "rows": [[42]],
    }
    assert _trace_view(view)["python"] == view["python"]


TESTS = [
    value
    for name, value in sorted(globals().items())
    if name.startswith("test_") and callable(value)
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
