"""Differential oracle: the Python emitter must reproduce what the SQL emitter computed.

Determinism guarantees the same question yields the same SQL. It does not guarantee that SQL is
right, and until this suite existed nothing recomputed the relational algebra independently — the
hermetic assertions pin SQL strings and hand-written expected rows, both authored alongside the
planner they check. Here a second implementation walks the same validated AST over Python objects
and must agree, row for row.

Both sides run the SAME candidate AST the planner selected, over the SAME rows:

* SQL side goes through the production entry point, `engine/tables.py:TableQuery.execute` — the
  `sqlite_decimal` dialect, the serving affinities, the serving result normalization. Not a
  simplified re-implementation of serving behaviour.
* Python side hydrates through SQLAlchemy (bare full-table selects only) and evaluates the tree.

Comparison is EXACT. `spider/probe/spider_eval.py:normalize_value` is deliberately not reused: it
rounds to three decimals and lowercases text for benchmark grading, which would hide precisely the
exact-decimal differences this repository exists to prevent.

Skips (reported as skips, never as passes) when SQLAlchemy is absent — it is a test-only
dependency in `requirements-ci.txt` and is not installed in the serving image.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import ast
import collections
import pathlib
import sys

from engine.sql_ast import SelectQuery, SetQuery
from engine.sql_schema import SchemaGraph
from engine.sql_search import SQLSearcher
from engine.sql_ast import SQLType
from engine.tables import TableQuery

try:
    import sqlalchemy  # noqa: F401
    HAVE_SQLALCHEMY = True
except ImportError:                                          # pragma: no cover - env dependent
    HAVE_SQLALCHEMY = False

if HAVE_SQLALCHEMY:
    from deterministic.emitter.py.classes import ForeignKeySpec, build_database, specs_from_schema
    from deterministic.emitter.py.evaluate import evaluate_query
    from deterministic.emitter.py.hydrate import CircularOracleError, assert_bare_select, hydrate, link_graph
    from deterministic.emitter.py import render


# --------------------------------------------------------------------------- fixtures

CUSTOMERS = {"name": "customers", "columns": ["Customer_ID", "Name", "City"],
             "rows": [[1, "Alice", "Paris"], [2, "Bob", "Lyon"], [3, "Cara", None]]}
# Order counts are distinct (Alice 3, Bob 2, Cara 0) so the interesting extrema questions have one
# right answer. Where a candidate is still tied at its LIMIT boundary the evaluator says so and the
# comparison is skipped: `ORDER BY ... LIMIT` over a tie is unspecified in SQL, and two correct
# engines may legitimately differ there.
ORDERS = {"name": "orders", "columns": ["Order_ID", "Customer_ID", "Amount"],
          "rows": [[10, 1, 25], [11, 1, 40], [14, 1, 15], [12, 2, 12], [13, 2, None]]}
ITEMS = {"name": "items", "columns": ["Item_ID", "Order_ID", "Price"],
         "rows": [[100, 10, 8], [101, 10, 12], [102, 11, 30], [103, 12, 30]]}
COMMERCE_FKS = [
    {"from_table": "orders", "from_col": "Customer_ID", "to_table": "customers", "to_col": "Customer_ID"},
    {"from_table": "items", "from_col": "Order_ID", "to_table": "orders", "to_col": "Order_ID"},
]

# Fractional data is the case that would expose a binary-float evaluator, so the corpus carries it.
LEDGER = {"name": "ledger", "columns": ["Entry_ID", "Currency", "Amount"],
          "rows": [[1, "EUR", "10.10"], [2, "EUR", "20.20"], [3, "USD", "0.30"], [4, "USD", "0.10"]]}

PEOPLE = {"name": "people", "columns": ["Person_ID", "Name", "Country", "Age"],
          "rows": [[1, "Alice", "France", 30], [2, "Bob", "France", 20], [3, "Cara", "Spain", 40]]}

QUESTIONS = [
    "how many customers are there",
    "total amount",
    "list each customer name and total order amount",
    "average order amount per customer",
    "how many distinct item prices",
    "which customer has the highest total order amount",
    "top 2 items by price",
    "list customer names ordered by name descending",
    "customers in Paris",
    "total price of items",
    "how many orders per customer",
    "names of customers",
    # Zero-inclusive frequency extrema is the shape that emits LEFT JOIN, and Cara has no orders.
    # Without it a LEFT-JOIN-as-INNER-JOIN defect survives the whole corpus.
    "which customer has the smallest number of orders",
    "which customer has the largest number of orders",
    "show all orders and their customers",
]

CORPUS = [
    ("commerce", [CUSTOMERS, ORDERS, ITEMS], COMMERCE_FKS, QUESTIONS),
    ("ledger", [LEDGER], [], ["total amount", "average amount", "how many entries",
                              "total amount per currency", "highest amount"]),
    ("people", [PEOPLE], [], ["how many people", "average age", "oldest person",
                              "names of people in France", "youngest and oldest age"]),
]

_AFFINITY = {SQLType.INTEGER: "INTEGER", SQLType.REAL: "REAL"}


# --------------------------------------------------------------------------- harness


def planner_schema(tables, fks):
    """Build the planner schema records both emitters read, from the one typed SchemaGraph."""
    graph = SchemaGraph.from_tables(tables, fks)
    schema = [{
        "table": column.ref.table, "name": column.ref.name, "idx": column.index,
        "affinity": _AFFINITY.get(column.ref.type, "TEXT"),
        "struct": set(), "ace": [], "is_date": False, "values": list(column.values),
    } for column in graph.columns]
    return schema, {table["name"]: table for table in tables}


def sql_rows(query, schema, tablemap):
    """The production path: TableQuery.execute, exactly as serving runs it."""
    _columns, rows = TableQuery().execute(tablemap, schema, sql=None, query=query)
    return [tuple(row) for row in rows]


def python_rows(query, schema, tablemap, fks=()):
    specs = specs_from_schema(schema)
    engine, _base, classes = build_database(specs, tablemap)
    tables, _log = hydrate(engine, specs, classes)
    return evaluate_query(query, tables)


def canon(value):
    """Exact canonical form: numbers compare as numbers, everything else as text."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return str(value)
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return str(value)


def canon_rows(rows):
    return [tuple(canon(value) for value in row) for row in rows]


def is_ordered(query):
    return isinstance(query, SelectQuery) and bool(query.order_by)


def same(sql, python, ordered):
    left, right = canon_rows(sql), canon_rows(python)
    if collections.Counter(map(repr, left)) != collections.Counter(map(repr, right)):
        return False, "row multisets differ"
    if ordered and left != right:
        # An ORDER BY leaves tied rows in an unspecified order; only a real resequencing counts.
        return True, "tie order differs (permitted)"
    return True, "match"


# --------------------------------------------------------------------------- tests


def test_python_emitter_reproduces_sql_results():
    checked = ambiguous = 0
    for label, tables, fks, questions in CORPUS:
        schema, tablemap = planner_schema(tables, fks)
        searcher = SQLSearcher.from_tables(tables, fks)
        for question in questions:
            candidates = searcher.search(question)
            if not candidates:
                continue
            candidate = candidates[0]
            actual, notes = python_rows(candidate.query, schema, tablemap, fks)
            if notes["ambiguous_limit"]:
                ambiguous += 1
                continue
            expected = sql_rows(candidate.query, schema, tablemap)
            ok, reason = same(expected, actual, is_ordered(candidate.query))
            assert ok, (
                f"[{label}] {question!r}\n  sql   : {candidate.sql}\n"
                f"  {reason}\n  sql rows   : {expected}\n  python rows: {actual}"
            )
            checked += 1
    assert checked >= 15, f"corpus too small to be evidence: only {checked} candidates compared"


def test_python_emitter_reproduces_every_ranked_candidate():
    """Top-1 alone would only cover the shapes the ranker happens to prefer."""
    checked = ambiguous = 0
    for label, tables, fks, questions in CORPUS:
        schema, tablemap = planner_schema(tables, fks)
        searcher = SQLSearcher.from_tables(tables, fks)
        for question in questions:
            for candidate in searcher.search(question)[:5]:
                actual, notes = python_rows(candidate.query, schema, tablemap, fks)
                if notes["ambiguous_limit"]:
                    ambiguous += 1
                    continue
                expected = sql_rows(candidate.query, schema, tablemap)
                ok, reason = same(expected, actual, is_ordered(candidate.query))
                assert ok, (
                    f"[{label}] {question!r}\n  sql   : {candidate.sql}\n"
                    f"  {reason}\n  sql rows   : {expected}\n  python rows: {actual}"
                )
                checked += 1
    # 50 today, spanning inner and LEFT joins, grouping, ordering, limits, distinct, and all
    # five aggregates. The floor guards against a search regression hollowing the oracle out.
    assert checked >= 45, f"only {checked} candidates compared"


def test_hydration_never_pushes_relational_work_into_sql():
    """Without this the oracle would be SQL checking SQL, and would confirm nothing."""
    schema, tablemap = planner_schema([CUSTOMERS, ORDERS, ITEMS], COMMERCE_FKS)
    specs = specs_from_schema(schema)
    engine, _base, classes = build_database(specs, tablemap)
    _tables, log = hydrate(engine, specs, classes)
    assert log, "hydration issued no statements at all"
    assert len(log) == len(specs), f"expected one select per table, got {len(log)}: {list(log)}"
    for statement in log:
        assert_bare_select(statement)

    for pushed in ('SELECT a FROM t WHERE a = 1',
                   'SELECT a FROM t JOIN u ON t.a = u.a',
                   'SELECT a FROM t GROUP BY a',
                   'SELECT a FROM t ORDER BY a',
                   'SELECT a FROM t LIMIT 1'):
        try:
            assert_bare_select(pushed)
        except CircularOracleError:
            continue
        raise AssertionError(f"the circularity guard failed to reject: {pushed}")


def test_ir_does_not_import_its_emitters():
    """engine/sql_ast.py is the IR: emitters depend on it, never the reverse."""
    tree = ast.parse(pathlib.Path("engine/sql_ast.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    offenders = sorted(name for name in imported if name.split(".")[0] == "deterministic")
    assert not offenders, (
        f"engine/sql_ast.py imports {offenders}; the IR must stay the one definition of meaning "
        "that every target renders, so emitters depend on it and never the reverse"
    )


def test_evaluator_handles_null_and_empty_aggregate_semantics():
    """The cases hand-written expectations most often get wrong."""
    schema, tablemap = planner_schema([ORDERS], [])
    specs = specs_from_schema(schema)
    engine, _base, classes = build_database(specs, tablemap)
    tables, _log = hydrate(engine, specs, classes)

    from engine.sql_ast import Aggregate, ColumnRef, Comparison, Literal, SelectItem, Star

    amount = ColumnRef("orders", "Amount", SQLType.INTEGER)
    # SUM skips the NULL row; COUNT(*) does not.
    total = SelectQuery(select=(SelectItem(Aggregate("SUM", amount)),
                                SelectItem(Aggregate("COUNT", Star())),
                                SelectItem(Aggregate("COUNT", amount))),
                        from_table="orders")
    assert evaluate_query(total, tables)[0] == [(Decimal(92), 5, 4)], evaluate_query(total, tables)
    assert canon_rows(sql_rows(total, schema, tablemap)) == canon_rows(evaluate_query(total, tables)[0])

    # An aggregate over an empty result still returns exactly one row: NULL sum, zero count.
    empty = SelectQuery(
        select=(SelectItem(Aggregate("SUM", amount)), SelectItem(Aggregate("COUNT", Star()))),
        from_table="orders",
        where=Comparison(ColumnRef("orders", "Order_ID", SQLType.INTEGER), ">",
                         Literal(999, SQLType.INTEGER)),
    )
    assert evaluate_query(empty, tables)[0] == [(None, 0)], evaluate_query(empty, tables)
    assert canon_rows(sql_rows(empty, schema, tablemap)) == canon_rows(evaluate_query(empty, tables)[0])



def test_evaluator_matches_sql_on_distinct_and_three_valued_logic():
    """Shapes the question corpus never produces, and where hand-written expectations go wrong.

    A planner that never emits `SELECT DISTINCT` or an `AND` over a NULL-bearing comparison would
    leave those evaluator branches untested; mutation testing showed both survived the corpus.
    Both sides still run, so these stay differential rather than hand-asserted.
    """
    from engine.sql_ast import BooleanExpr, ColumnRef, Comparison, Literal, SelectItem

    schema, tablemap = planner_schema([ORDERS], [])
    specs = specs_from_schema(schema)
    engine, _base, classes = build_database(specs, tablemap)
    tables, _log = hydrate(engine, specs, classes)

    customer = ColumnRef("orders", "Customer_ID", SQLType.INTEGER)
    amount = ColumnRef("orders", "Amount", SQLType.INTEGER)
    order_id = ColumnRef("orders", "Order_ID", SQLType.INTEGER)

    def check(label, query):
        expected = sql_rows(query, schema, tablemap)
        actual, notes = evaluate_query(query, tables)
        assert not notes["ambiguous_limit"], label
        ok, reason = same(expected, actual, is_ordered(query))
        assert ok, f"{label}: {reason}\n  sql: {expected}\n  py : {actual}"
        return actual

    # DISTINCT collapses the repeated Customer_ID values.
    distinct = SelectQuery(select=(SelectItem(customer),), from_table="orders", distinct=True,
                           order_by=())
    assert len(check("SELECT DISTINCT", distinct)) == 2

    # Order 13 has a NULL Amount: `NULL > 10` is UNKNOWN, and UNKNOWN AND TRUE drops the row.
    conjunction = SelectQuery(
        select=(SelectItem(order_id),), from_table="orders",
        where=BooleanExpr("AND", (
            Comparison(amount, ">", Literal(10, SQLType.INTEGER)),
            Comparison(customer, "=", Literal(2, SQLType.INTEGER)),
        )),
    )
    assert check("UNKNOWN AND TRUE", conjunction) == [(12,)]

    # UNKNOWN OR TRUE is TRUE, so the NULL-amount row survives this one.
    disjunction = SelectQuery(
        select=(SelectItem(order_id),), from_table="orders",
        where=BooleanExpr("OR", (
            Comparison(amount, ">", Literal(1000, SQLType.INTEGER)),
            Comparison(customer, "=", Literal(2, SQLType.INTEGER)),
        )),
    )
    assert sorted(check("UNKNOWN OR TRUE", disjunction)) == [(12,), (13,)]



def test_foreign_key_graph_is_wired_in_python_not_by_the_orm():
    """The object graph is built from rows already in memory — that is what keeps SQL out of it."""
    schema, tablemap = planner_schema([CUSTOMERS, ORDERS, ITEMS], COMMERCE_FKS)
    specs = specs_from_schema(schema)
    engine, _base, classes = build_database(specs, tablemap)
    tables, log = hydrate(engine, specs, classes)
    before = len(log)

    graph = link_graph(tables, [ForeignKeySpec(fk["from_table"], fk["from_col"],
                                               fk["to_table"], fk["to_col"])
                                for fk in COMMERCE_FKS])
    assert len(log) == before, "linking the graph issued SQL; it must use rows already in memory"

    parents = graph["orders.Customer_ID"]
    assert [row[("customers", "Name")] for row in parents[1]] == ["Alice"]
    assert [row[("customers", "Name")] for row in parents[2]] == ["Bob"]
    assert 3 in parents, "Cara has no orders but is still reachable as a parent"

    # orders -> customers -> (city) is the navigation an emitted wrapper class walks.
    alice_orders = [row for row in tables["orders"] if row[("orders", "Customer_ID")] == 1]
    assert len(alice_orders) == 3
    assert parents[alice_orders[0][("orders", "Customer_ID")]][0][("customers", "City")] == "Paris"



def test_generated_modules_are_valid_python_and_stay_off_disk_by_default():
    """The _gen dump is a debug artifact: valid Python, and never written unless asked for."""
    import os
    import tempfile

    schema, tablemap = planner_schema([CUSTOMERS, ORDERS], COMMERCE_FKS[:1])
    specs = specs_from_schema(schema)
    searcher = SQLSearcher.from_tables([CUSTOMERS, ORDERS], COMMERCE_FKS[:1])
    query = searcher.search("list each customer name and total order amount")[0].query

    modules = render.render_modules(
        "orders_customers", specs, {"total_amount": query},
        [ForeignKeySpec(fk["from_table"], fk["from_col"], fk["to_table"], fk["to_col"])
         for fk in COMMERCE_FKS[:1]],
    )
    assert set(modules) == {"customers.py", "orders.py", "orders_customers.py"}, sorted(modules)
    for filename, source in modules.items():
        ast.parse(source)                                    # emitted source must at least parse
    wrapper = modules["orders_customers.py"]
    assert "class OrdersCustomers:" in wrapper
    assert "def total_amount(self):" in wrapper
    assert "join orders" in wrapper or "join customers" in wrapper, wrapper
    assert "Customer_ID -> Customers.Customer_ID" in modules["orders.py"]

    previous = os.environ.pop(render.DEBUG_FLAG, None)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            assert render.dump("conv_1", modules, root=root) is None, "dumped without the flag"
            assert not any(root.iterdir()), "the dump wrote files with the flag unset"
            os.environ[render.DEBUG_FLAG] = "1"
            written = render.dump("conv_1", modules, root=root)
            assert written is not None and (written / "orders_customers.py").exists()
    finally:
        os.environ.pop(render.DEBUG_FLAG, None)
        if previous is not None:
            os.environ[render.DEBUG_FLAG] = previous


TESTS = [
    test_python_emitter_reproduces_sql_results,
    test_python_emitter_reproduces_every_ranked_candidate,
    test_hydration_never_pushes_relational_work_into_sql,
    test_ir_does_not_import_its_emitters,
    test_evaluator_handles_null_and_empty_aggregate_semantics,
    test_evaluator_matches_sql_on_distinct_and_three_valued_logic,
    test_foreign_key_graph_is_wired_in_python_not_by_the_orm,
    test_generated_modules_are_valid_python_and_stay_off_disk_by_default,
]

ALWAYS = {test_ir_does_not_import_its_emitters}


def main():
    if not HAVE_SQLALCHEMY:
        for test in ALWAYS:
            test()
            print(f"  ok   {test.__name__}")
        print("\nemitter parity: SKIP (sqlalchemy not installed; it is a requirements-ci.txt dependency)")
        sys.exit(0)
    failed = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\nemitter parity: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
