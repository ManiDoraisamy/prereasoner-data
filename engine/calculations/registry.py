"""Single registry and serving policy for deterministic calculations."""
from __future__ import annotations

from typing import Any, Sequence

from engine.calculations.core import (
    ComputationEvidence,
    branch_realizes_plan,
    describe_computation,
)
from engine.calculations.specifications import SPECIFICATIONS
from engine.sql_ast import Query
from engine.sql_schema import SchemaGraph


_BY_NAME = {specification.name: specification for specification in SPECIFICATIONS}
if len(_BY_NAME) != len(SPECIFICATIONS):
    raise RuntimeError("calculation specification names must be unique")

CALCULATION_INTENT_PROTOTYPES = {
    "currency": "convert a monetary amount into a requested output currency",
    "ratio": "divide one numeric measure by another numeric measure",
    "rate_application": "apply a tax commission or interest rate to a monetary amount",
}


def specifications():
    return SPECIFICATIONS


def detect_calculations(question: str):
    """Return all explicit calculation intents in stable registry order."""
    return tuple(
        intent for specification in SPECIFICATIONS
        if (intent := specification.detect(question)) is not None
    )


def calculation_operand_queries(question: str) -> tuple[tuple[str, str], ...]:
    """Role-specific retrieval probes for the shared encoder."""
    return tuple(
        (f"{intent.specification}:{role}", f"{phrase} | {question}")
        for intent in detect_calculations(question)
        for role, phrase in sorted(intent.operands.items())
    )


def calculation_operand_scores(intent, semantic_signals):
    available = getattr(semantic_signals, "calculation_operands", {}) or {}
    return {
        role: available.get(f"{intent.specification}:{role}", {})
        for role in intent.operands
    }


def plans_for(question: str, graph: SchemaGraph, semantic_signals=None):
    return tuple(
        plan
        for intent in detect_calculations(question)
        for plan in _BY_NAME[intent.specification].plans(
            intent, graph, calculation_operand_scores(intent, semantic_signals)
        )
    )


def assess_calculations(
    question: str,
    tables,
    graph: SchemaGraph,
    evidence: ComputationEvidence,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        _BY_NAME[intent.specification].assess(intent, evidence, tables, graph)
        for intent in detect_calculations(question)
    )


def _query_realizes_plan(query: Query, plan, graph: SchemaGraph) -> bool:
    evidence = describe_computation(query)
    return bool(evidence.branches) and all(
        branch_realizes_plan(branch, plan, graph) for branch in evidence.branches
    )


def calculation_rank_features(
    question: str,
    query: Query,
    graph: SchemaGraph,
    semantic_signals=None,
) -> tuple[tuple[str, float], ...]:
    """Inspectable preference only; release admissibility remains post-ranking."""
    features = []
    for intent in detect_calculations(question):
        if intent.operation == "filter":
            continue
        plans = _BY_NAME[intent.specification].plans(
            intent, graph, calculation_operand_scores(intent, semantic_signals)
        )
        if not plans:
            continue
        realized = any(_query_realizes_plan(query, plan, graph) for plan in plans)
        features.append((
            f"calculation:{intent.specification}:{intent.operation}",
            8.0 if realized else -8.0,
        ))
    return tuple(features)


def select_calculation_candidate(
    question: str,
    tables,
    graph: SchemaGraph,
    candidates: Sequence,
):
    """Choose the first ranked candidate satisfying every explicit calculation intent.

    Scores and candidate ordering are untouched.  If no candidate is admissible, rank one is
    retained solely to provide concrete failed evidence in the clarification response.
    """
    ranked = tuple(candidates)
    intents = detect_calculations(question)
    if not ranked:
        return None, (), 0
    if not intents:
        return ranked[0], (), 0
    assessments = tuple(
        assess_calculations(question, tables, graph, describe_computation(candidate.query))
        for candidate in ranked
    )
    selected = next((
        index for index, rows in enumerate(assessments)
        if rows and all(row.get("status") == "satisfied" for row in rows)
    ), 0)
    return ranked[selected], assessments[selected], selected


def attach_calculation_evidence(response: dict, assessments) -> dict:
    """Attach one canonical list and the temporary currency compatibility projection."""
    rows = tuple(assessment for assessment in (assessments or ()) if assessment is not None)
    if not rows:
        response.pop("calculations", None)
        response.pop("currency", None)
        return response
    response["calculations"] = list(rows)
    currency = next((row for row in rows if row.get("specification") == "currency"), None)
    if currency is not None:
        response["currency"] = currency
    else:
        response.pop("currency", None)
    return response


def calculation_clarify(question: str, response: dict, assessments) -> dict:
    """Replace an uncertified numeric result with one structured calculation clarification."""
    failed = tuple(
        assessment for assessment in (assessments or ())
        if assessment is not None and assessment.get("status") != "satisfied"
    )
    if not failed:
        return response
    unmet = [
        {
            "name": f"calculation:{assessment['specification']}",
            "requested": assessment.get("target"),
            "detail": assessment.get("operation"),
            "available": assessment.get("available", assessment.get("available_targets", [])),
            "reason": assessment.get("reason", "calculation was not verified"),
        }
        for assessment in failed
    ]
    proposals = [assessment.get("proposal") for assessment in failed if assessment.get("proposal")]
    reason = "; ".join(dict.fromkeys(
        assessment.get("reason", "calculation was not verified") for assessment in failed
    ))
    result = {
        "question": question,
        "as_of": response.get("as_of"),
        "clarify": True,
        "original_sql": response.get("sql") or response.get("original_sql"),
        "proposed": proposals[0] if len(proposals) == 1 else "",
        "bindings": [
            {
                "token": assessment.get("phrase", ""),
                "kind": assessment.get("operation", "calculation"),
                "target": assessment.get("target"),
                "available": assessment.get("available", assessment.get("available_targets", [])),
            }
            for assessment in failed
        ],
        "dropped": [assessment.get("phrase", "") for assessment in failed],
        "unmet": unmet,
        "reason": reason,
        "calculations": list(assessments),
        "result": None,
        "error": None,
        "model": "engine - clarify (typed calculation semantics not satisfied)",
    }
    if response.get("clarify") and response.get("model") != result["model"]:
        result["prior_clarification"] = {
            "model": response.get("model"),
            "dropped": list(response.get("dropped") or ()),
            "reason": response.get("reason"),
        }
    currency = next((row for row in assessments if row.get("specification") == "currency"), None)
    if currency is not None:
        result["currency"] = currency
    return result
