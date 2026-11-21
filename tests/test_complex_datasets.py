"""Release gate for the shipped non-linear decomposition fixtures.

The production own-data selection (the Decisions path, engine/tables.py select_query) answers every leaf, so a test
that reads a leaf needs the runtime bundle and the Decisions API's key, and one served through the production entry
also the live knowledgebase; each is skipped, as skipped, without them. The
compiler fuses the leaves' programs once, emits SQL and readable Python from that one DAG, and executes both on the
request's database. Expected rows are fixture-authored gold values, not values copied from either backend.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine

from engine.decomposition import build_decomposed_plan, shown_output
from engine.deterministic import DeterministicAnalysis
from engine.deterministic.context import analysis_execution_context
from engine.encoder_overlay import EncoderQuery
from tests.test_datasets import DATASET_DIR, EXPECTED, _tables


COMPLEX_DATASETS = ("complex-promotions", "complex-category-gaps",
                    "complex-promotions-xlsx", "complex-unsold-products")
# The prompts with several goals ("the top 3 ... and the top 2 ..., then list each pair ..."), longer than one query
# is taken to answer (engine/decomposition.py ONE_QUERY_WORDS); the Decisions path asks back about each, so each
# asks for the decomposition proposal. The
# unsold-products prompt asks one thing, the rows no referencing row matches.
COMPOUND_DATASETS = ("complex-promotions", "complex-category-gaps", "complex-promotions-xlsx")


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


def _run_fixture(planner: EncoderQuery, directory: Path, proposal: dict | None = None,
                 question: str | None = None, raw_tables: list[dict] | None = None):
    """Compile `proposal` (default: the shipped fixture) for `question` (default: the prompt)
    over `raw_tables` (default: the fixture's tables in file-name order), on one request database whose tables connect
    by their keys, as serving does (engine/prepared.py link_tables). Returns the plan, its result and the workbooks its
    leaves read (build_decomposed_plan's catalogs, which name the answer's columns as serving does)."""
    from sqlalchemy.pool import StaticPool

    from engine.prepared import SharedConnection, link_tables, scratch_database

    raw_tables = raw_tables or _tables(directory)
    tables, foreign_keys = planner.ingest(raw_tables)
    proposal = proposal or json.loads(
        (directory / "decomposition.json").read_text(encoding="utf-8")
    )
    catalogs: list = []
    with scratch_database() as database:
        link_tables(database, planner)
        plan = build_decomposed_plan(
            planner,
            directory.name.replace("-", "_"),
            tables,
            foreign_keys,
            proposal,
            question=question or (directory / "prompt.txt").read_text(encoding="utf-8").strip(),
            catalogs=catalogs,
        )
        shared = SharedConnection(database.connection)
        engine = create_engine("sqlite+pysqlite://", creator=lambda: shared, poolclass=StaticPool)
        result = DeterministicAnalysis(plan, conversation_schema="main", dataset_version="fixture").run(
            engine,
            mode="verify",
            estimated_rows=sum(len(table["rows"]) for table in tables),
            python_row_limit=10_000,
        )
    return plan, result, catalogs


def test_a_decomposition_cannot_invent_a_cutoff():
    """The Chrome pass (2026-09-24) caught this. For "List the product names that no customer
    from Paris has bought" the model crossed Paris customers with products. The engine answered
    that cross inputs need explicit limits, the model resubmitted with "the top 100" on both
    lists, and 14 customer-product pairs were served: a question nobody asked. The replayed
    production proposal is now rejected with the cutoff named, so the model gets the correction
    instead of the user getting the pairs."""
    from engine.decomposition import DecompositionError

    directory = DATASET_DIR / "complex-unsold-products"
    capped = {
        "subquestions": [
            {"id": "products", "question": "List the top 100 product names."},
            {"id": "paris_customers", "question": "List the top 100 customer names from Paris."},
            {"id": "purchases",
             "question": "List each customer name and the product name they have bought."},
        ],
        "merges": [
            {"id": "candidate_pairs", "op": "cross", "inputs": ["paris_customers", "products"]},
            {"id": "not_purchased", "op": "anti_join", "inputs": ["candidate_pairs", "purchases"]},
        ],
        "output": "not_purchased",
        "grain": "one Paris customer-product pair not purchased",
    }
    planner = EncoderQuery()
    try:
        _run_fixture(planner, directory, capped)
    except DecompositionError as exc:
        assert "100" in str(exc) and "does not state" in str(exc), exc
    else:
        raise AssertionError("an invented cutoff was compiled and answered")


def test_shipped_complex_datasets_match_gold_in_python_and_sql():
    planner = EncoderQuery()
    for name in COMPLEX_DATASETS:
        plan, result, catalogs = _run_fixture(planner, DATASET_DIR / name)
        kind, expected = EXPECTED[name]
        assert kind == "own-rows"
        # the answer as serving returns it (engine/knowledge_compose.py): each column named by the header of the sheet
        # column it holds, each key in the sheet's spelling
        columns = list(plan.view_columns()[plan.output])
        names, rows = shown_output(plan, columns, [tuple(row[column] for column in columns) for row in result.rows], catalogs)
        actual = [[row[names.index(column)] for column in expected["columns"]] for row in rows]
        assert actual == expected["rows"], (name, names, actual)
        assert result.mode.value == "verify"
        # `verify` reaches this line only after DeterministicAnalysis has compared
        # every materialized Python stage with its corresponding SQL stage.
        proposal = json.loads(
            (DATASET_DIR / name / "decomposition.json").read_text(encoding="utf-8")
        )
        record = result.record()
        assert len(record["manifest"]["sections"]) == (
            len(proposal["subquestions"]) + len(proposal["merges"])
        )
        output_merge = next(
            merge for merge in proposal["merges"] if merge["id"] == proposal["output"]
        )
        assert record["views"][-1]["is_output"] is True
        assert record["views"][-1]["section_inputs"] == list(output_merge["inputs"])
        assert ".anti_join(" in record["views"][-1]["python"]
        assert "NOT EXISTS" in record["views"][-1]["sql"]


def test_full_complex_prompts_request_decomposition_without_executing_a_partial_answer():
    from engine.prepared import link_tables, scratch_database

    planner = EncoderQuery()
    for name in COMPOUND_DATASETS:
        directory = DATASET_DIR / name
        question = (directory / "prompt.txt").read_text(encoding="utf-8").strip()
        descriptor = {"slug": name.replace("-", "_"), "revision": 1}
        with analysis_execution_context(descriptor, "c_" + "8" * 32), patch.object(
            planner,
            "execute",
            side_effect=AssertionError("a compound probe executed a partial SQL answer"),
        ) as execute, scratch_database() as database:
            link_tables(database, planner)        # the tables connect by their keys, as serving connects them
            response = planner.serve(_tables(directory), question)
        assert response.get("decomposition_required"), (name, response)
        assert response.get("result") is None and response.get("error") is None
        assert execute.call_count == 0


def test_compose_surface_does_not_swallow_a_compound_question():
    """The Chrome pass caught this: 'top 3 ... and top 2 ...' satisfies the COMPOSE gate,
    so the live path built a partial top-N and answered it — the delegate's compound
    trigger (one layer down) never ran. The compose arm must consult the same
    execution-free probe before composing."""
    from engine.decomposition import search_probe
    from engine.prepared import link_tables, scratch_database

    planner = EncoderQuery()
    for name in COMPOUND_DATASETS:
        directory = DATASET_DIR / name
        question = (directory / "prompt.txt").read_text(encoding="utf-8").strip()
        with patch.object(
            planner,
            "execute",
            side_effect=AssertionError("the compound probe executed a query"),
        ), scratch_database() as database:
            link_tables(database, planner)        # the tables connect by their keys, as serving connects them
            required = search_probe(planner, _tables(directory), question).decomposition_required
        assert required is not None, name
        assert "compound" in required["reason"], name
    # A genuinely simple question must NOT trigger the probe, or every composed
    # top-N would bounce to Gemini for a pointless proposal.
    simple = search_probe(
        planner, _tables(DATASET_DIR / "complex-promotions"),
        "total quantity by product name",
    )
    assert simple.decomposition_required is None


def test_the_serving_entry_point_carries_the_proposal_to_the_compose_layer():
    """engine/server.py calls KnowledgeReasoner.serve(..., decomposition=...). That
    signature never accepted the keyword, so the retry raised TypeError and returned 500
    before reaching any decomposition code — invisible to every test that called the
    compose or planner layers directly. Assert the real entry point's contract."""
    import inspect

    from engine.knowledge import KnowledgeReasoner
    from engine.knowledge_compose import ComposedKnowledgeQuery

    entry = inspect.signature(KnowledgeReasoner.serve).parameters
    assert "decomposition" in entry, "the serving entry point must accept a proposal"

    # ...and must FORWARD it, not merely accept it.
    forwarded = {}

    class Recorder(ComposedKnowledgeQuery):
        def __init__(self):
            pass

        def serve(self, *args, **kwargs):
            forwarded.update(kwargs)
            return {"question": "q", "result": None, "clarify": None, "error": None}

    reasoner = KnowledgeReasoner.__new__(KnowledgeReasoner)
    reasoner.composed = Recorder()

    class _Qw:
        def begin_request(self): pass
        def link_request(self, as_of=None, explicit_fks=()): pass
        def begin_typing(self): pass
        def take_typing(self): return {}
        def end_request(self): pass

    import contextlib

    from engine import knowledge

    reasoner.qw = _Qw()
    proposal = {"subquestions": [], "merges": [], "output": "x", "grain": "one row"}
    with patch.object(knowledge, "request_database", contextlib.nullcontext),             patch.object(KnowledgeReasoner, "_tag_present", lambda self, r, *a: r),          patch.object(KnowledgeReasoner, "_verify_calculations", lambda self, r, *a: r),          patch.object(KnowledgeReasoner, "_attach_typing", lambda self, r, *a: r):
        reasoner.serve([], "top 3 x and top 2 y", "c_" + "0" * 32, decomposition=proposal)
    assert forwarded.get("decomposition") is proposal


def test_a_negated_existence_prompt_is_answered_as_one_query():
    """"List the product names that no customer from Paris has bought" asks for the rows no referencing row matches:
    one query, the products whose name is not among the names Paris customers bought, answered directly with the
    rows derived from the CSVs, with no decomposition offered or asked for and no clarification. Served through the
    production entry (engine/knowledge.py KnowledgeReasoner.serve, as engine/server.py serves it: regress/live_schema.py
    served), so the request's key and world links are registered as serving registers them; a request that links only
    the keys reads a workbook production never builds, and the choice between "no Paris line holds it" and "no line
    other than Paris holds it" turns on the columns the workbook holds."""
    from engine.knowledge import KnowledgeReasoner
    from regress.live_schema import live_schema, served

    directory = DATASET_DIR / "complex-unsold-products"
    question = (directory / "prompt.txt").read_text(encoding="utf-8").strip()
    schema = live_schema().name
    response = served(schema, KnowledgeReasoner().serve, _tables(directory), question, schema)
    assert not response.get("decomposition_required") and not response.get("clarify"), response
    _, expected = EXPECTED["complex-unsold-products"]
    assert [list(row) for row in response["result"]["rows"]] == expected["rows"], response["result"]


TESTS = [
    test_the_serving_entry_point_carries_the_proposal_to_the_compose_layer,
    test_compose_surface_does_not_swallow_a_compound_question,
    test_full_complex_prompts_request_decomposition_without_executing_a_partial_answer,
    test_shipped_complex_datasets_match_gold_in_python_and_sql,
    test_a_decomposition_cannot_invent_a_cutoff,
    test_a_negated_existence_prompt_is_answered_as_one_query,
]


# Load the runtime bundle (the planner's schema); the second group also reads questions through the Decisions API.
MODEL_BACKED = (
    test_a_decomposition_cannot_invent_a_cutoff,
)
DECISIONS_BACKED = (     # each serves a question through the one selection, which reads it through the Decisions API
    test_full_complex_prompts_request_decomposition_without_executing_a_partial_answer,
    test_shipped_complex_datasets_match_gold_in_python_and_sql,
    test_compose_surface_does_not_swallow_a_compound_question,
)


def runtime_bundle_missing() -> str | None:
    """Why the real planner cannot load here, or None. The weights are external (fetched by
    engine.fetch_weights and pinned by the manifest), and public CI installs no model stack."""
    import importlib.util

    if importlib.util.find_spec("torch") is None:
        return "the model stack (torch) is not installed"
    from engine.artifact_provenance import validate_weight_bundle
    from engine.config import DATA_DIR

    try:
        validate_weight_bundle(DATA_DIR)
    except RuntimeError as exc:
        return f"the runtime bundle is not provisioned ({exc})"
    return None


LIVE_BACKED = (     # served through the production entry on the live knowledgebase (the Cloud SQL proxy) and the Decisions API
    test_a_negated_existence_prompt_is_answered_as_one_query,
)


def live_missing() -> str | None:
    """Why the live knowledgebase cannot be read here, or None."""
    import os

    return None if os.environ.get("KB_PG_PASSWORD") else "the live knowledgebase is not reachable here (KB_PG_PASSWORD)"


def decisions_missing() -> str | None:
    """Why the Decisions API cannot be called here, or None (engine/llm.py reads the deployment's key)."""
    from engine.config import decisions_key

    return None if decisions_key() else "the Decisions API has no key here (GOOGLE_API_KEY)"


def main() -> int:
    failed, skipped = [], []
    missing = runtime_bundle_missing()
    unread = missing or decisions_missing()
    for test in TESTS:
        why = (missing if test in MODEL_BACKED else unread if test in DECISIONS_BACKED
               else (unread or live_missing()) if test in LIVE_BACKED else None)
        if why:
            skipped.append(test.__name__)
            print(f"  SKIP {test.__name__}: {why}")
            continue
        try:
            test()
            print(f"  ok   {test.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed.append(test.__name__)
            print(f"  FAIL {test.__name__}: {type(exc).__name__}: {exc}")
    passed = len(TESTS) - len(failed) - len(skipped)
    print(f"\ncomplex datasets: {passed} passed, {len(failed)} failed, {len(skipped)} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
