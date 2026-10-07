"""Observed storage keeps large identifiers and scientific decimals usable and exact."""
import unittest
from decimal import Decimal

from engine.numeric import observed_numeric_affinity, json_dumps
from engine.tables import TableQuery


class NumericStorageTests(unittest.TestCase):
    def test_schema_uses_storage_bounds_for_observed_values(self):
        import numpy as np
        planner = TableQuery()
        planner.dims = [{"name":"is_num", "family":"struct", "dim_id":0}]
        planner._encode = lambda names: np.ones((len(names), 1))
        planner._layers = lambda units, encoded: [encoded]
        schema, _, _ = planner.schema([{"name":"ledger", "columns":["id","amount"],
            "rows":[[str(10**30),'1e-3'],[str(10**30+1),'2e-3']]}], [])
        self.assertEqual([c['affinity'] for c in schema], ['REAL','REAL'])

    def test_browser_wire_and_live_trace_keep_every_integer_digit(self):
        import json
        from engine.trace import rtdb_safe
        payload = {'rows': [[10**30+1, Decimal(10**30+2), 2**53-1, True]]}
        expected = {'rows': [[str(10**30+1), str(10**30+2), 2**53-1, True]]}
        self.assertEqual(json.loads(json_dumps(payload)), expected)
        self.assertEqual(rtdb_safe(payload), expected)

    def test_integer_boundaries_and_scientific_values(self):
        self.assertEqual(observed_numeric_affinity([-(2**63), 2**63-1]), "INTEGER")
        for values in ([2**63], [-(2**63)-1], [10**30], ["1e-3"], ["1e30"], ["1.0"]):
            self.assertEqual(observed_numeric_affinity(values), "REAL")
        for values in (["not recorded"], ["#DIV/0!"], ["1e100"], ["0.0000000000000000000001"]):
            self.assertEqual(observed_numeric_affinity(values), "TEXT")

    def test_a_comma_is_read_only_where_it_groups_digits(self):
        """Every comma was dropped, so a decimal comma read 100 times too large: "1,50" was 150, "12,34"
        1234 and "1.234,56" 123456 (release review, 2026-10-07). A comma that does not group digits leaves
        the cell text, and a column holding one is not a number column."""
        from engine.numeric import parse_decimal
        from engine.tables import csv_table
        for text, value in (("1,234", 1234), ("-1,234.56", Decimal("-1234.56")), ("12,34,567", 1234567),
                            ("$ 1,234", 1234), ("1,234%", 1234), ("+1,000,000", 1000000), ("1,000.", 1000)):
            self.assertEqual(parse_decimal(text), value, text)
        for text in ("1,50", "12,34", "1.234,56", "0,123", "1,2345", "1,,234", ",123", "1 234,56"):
            with self.assertRaises(ValueError, msg=text):
                parse_decimal(text)
        table = csv_table('item,amount,total\nA,"1,50","1,234"\nB,"12,34","2,500"\nC,"1.234,56","12,34,567"\n', "t")
        self.assertEqual(table["rows"], [["A", "1,50", 1234], ["B", "12,34", 2500], ["C", "1.234,56", 1234567]])
        self.assertEqual(observed_numeric_affinity([row[1] for row in table["rows"]]), "TEXT")
        self.assertEqual(observed_numeric_affinity([row[2] for row in table["rows"]]), "INTEGER")
        # A column mixing the formats is text: its grouped cells are not summed without the others.
        self.assertEqual(observed_numeric_affinity([1234, "1,50"]), "TEXT")

    def test_one_division_rounds_the_same_in_both_backends(self):
        """SQLite's decimal_div rounded half to even and decimal_avg left its mean unrounded, while the
        Python emitter rounds half up at DIVISION_SCALE places: 1 / 2097152 ended ...0312 in one and
        ...0313 in the other (release review, 2026-10-07). Both now divide with numeric.decimal_divide."""
        import sqlite3

        from engine.deterministic.operators import AVG, DIVIDE, FINALIZE_AVG, AverageState
        from engine.numeric import register_sqlite_decimal

        connection = sqlite3.connect(":memory:")
        register_sqlite_decimal(connection)
        self.assertEqual(connection.execute("SELECT decimal_div('1', '2097152')").fetchone()[0],
                         "0.00000047683715820313")
        for left, right in (("1", "2097152"), ("-1", "2097152"), ("2", "3"), ("10", "4")):
            stored = connection.execute("SELECT decimal_div(?, ?)", (left, right)).fetchone()[0]
            self.assertEqual(Decimal(stored), DIVIDE(Decimal(left), Decimal(right)), (left, right))
        connection.execute("CREATE TABLE t (v TEXT)")
        connection.executemany("INSERT INTO t VALUES (?)", [("1",), ("2",), ("2",)])
        state = AverageState()
        for value in (1, 2, 2):
            state = AVG(state, value)
        self.assertEqual(Decimal(connection.execute("SELECT decimal_avg(v) FROM t").fetchone()[0]),
                         FINALIZE_AVG(state))

    def test_large_numeric_identifier_does_not_block_unrelated_measure(self):
        rows = [[str(10**30), "1e-3"], [str(10**30+1), "2e-3"]]
        schema = [{"table": "ledger", "name": name, "affinity": observed_numeric_affinity([r[i] for r in rows])}
                  for i, name in enumerate(("id", "amount"))]
        connection = TableQuery()._sqlite_tables({"ledger": {"columns": ["id", "amount"], "rows": rows}}, schema)
        try:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM ledger').fetchone()[0], 2)
            fetched = connection.execute('SELECT id, amount FROM ledger ORDER BY id').fetchall()
            self.assertEqual([str(row[0]) for row in fetched], [row[0] for row in rows])
            self.assertEqual(sum(Decimal(str(row[1])) for row in fetched), Decimal("0.003"))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
