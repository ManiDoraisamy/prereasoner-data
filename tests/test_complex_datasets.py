"""Hermetic release gate for the shipped non-linear decomposition fixtures.

The real promoted planner produces every leaf AST. The compiler then fuses those
leaves once, emits SQL and readable Python from that one DAG, and executes both
against the same in-memory SQLite snapshot. Expected rows are fixture-authored
gold values, not values copied from either backend.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine

from engine.decomposition import build_decomposed_plan
from engine.deterministic import DeterministicAnalysis
from engine.deterministic.context import analysis_execution_context
from engine.encoder_overlay import EncoderQuery
from tests.test_datasets import DATASET_DIR, EXPECTED, _tables


COMPLEX_DATASETS = ("complex-promotions", "complex-category-gaps")


def _sqlite_type(affinity) -> str:
    return {
        "INTEGER": "INTEGER",
        "REAL": "REAL",
        "NUMERIC": "NUMERIC",
        "DATE": "DATE",
        "BOOLEAN": "BOOLEAN",
    }.get(str(affinity).upper(), "TEXT")


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _run_fixture(planner: EncoderQuery, directory: Path):
    raw_tables = _tables(directory)
    tables, foreign_keys = planner.ingest(raw_tables)
    schema, _, _ = planner.schema(tables, foreign_keys)
    proposal = json.loads(
        (directory / "decomposition.json").read_text(encoding="utf-8")
    )
    plan = build_decomposed_plan(
        planner,
        directory.name.replace("-", "_"),
        tables,
        schema,
        foreign_keys,
        proposal,
    )
    source_tables = {table["name"]: table for table in tables}
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.connect() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS conversation")
        for table in plan.tables:
            definitions = ", ".join(
                f"{_quote(column.name)} {_sqlite_type(column.type.value)}"
                for column in table.columns
            )
            connection.exec_driver_sql(
                f"CREATE TABLE conversation.{_quote(table.name)} ({definitions})"
            )
            source = source_tables[table.name]
            placeholders = ", ".join("?" for _ in source["columns"])
            statement = (
                f"INSERT INTO conversation.{_quote(table.name)} "
                f"({', '.join(_quote(column) for column in source['columns'])}) "
                f"VALUES ({placeholders})"
            )
            connection.exec_driver_sql(statement, [tuple(row) for row in source["rows"]])
        result = DeterministicAnalysis(
            plan, conversation_schema="conversation"
        ).run(
            connection,
            mode="verify",
            estimated_rows=sum(len(table["rows"]) for table in tables),
            python_row_limit=10_000,
        )
    return plan, result


def test_shipped_complex_datasets_match_gold_in_python_and_sql():
    planner = EncoderQuery()
    for name in COMPLEX_DATASETS:
        plan, result = _run_fixture(planner, DATASET_DIR / name)
        kind, expected = EXPECTED[name]
        assert kind == "own-rows"
        actual = [
            [row[column] for column in expected["columns"]]
            for row in result.rows
        ]
        assert actual == expected["rows"], (name, actual)
        assert result.mode.value == "verify"
        # `verify` reaches this line only after DeterministicAnalysis has compared
        # every materialized Python stage with its corresponding SQL stage.
        record = result.record()
        assert len(record["manifest"]["sections"]) == 5
        assert record["views"][-1]["is_output"] is True
        assert record["views"][-1]["section_inputs"] == [
            "candidate_pairs", "purchased_pairs" if name == "complex-promotions" else "purchased_categories",
        ]
        assert ".anti_join(" in record["views"][-1]["python"]
        assert "NOT EXISTS" in record["views"][-1]["sql"]


def test_full_complex_prompts_request_decomposition_without_executing_a_partial_answer():
    planner = EncoderQuery()
    for name in COMPLEX_DATASETS:
        directory = DATASET_DIR / name
        question = (directory / "prompt.txt").read_text(encoding="utf-8").strip()
        descriptor = {"slug": name.replace("-", "_"), "revision": 1}
        with analysis_execution_context(descriptor, "c_" + "8" * 32), patch.object(
            planner,
            "execute",
            side_effect=AssertionError("a compound probe executed a partial SQL answer"),
        ) as execute:
            response = planner.serve(_tables(directory), question)
        assert response.get("decomposition_required"), (name, response)
        assert response.get("result") is None and response.get("error") is None
        assert execute.call_count == 0


TESTS = [
    test_full_complex_prompts_request_decomposition_without_executing_a_partial_answer,
    test_shipped_complex_datasets_match_gold_in_python_and_sql,
]


def main() -> int:
    failed = []
    for test in TESTS:
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\ncomplex datasets: {len(TESTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
