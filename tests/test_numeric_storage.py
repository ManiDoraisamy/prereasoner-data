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
