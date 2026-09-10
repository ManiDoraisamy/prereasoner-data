"""Request-local named-analysis context for optional dual execution."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class AnalysisExecutionContext:
    conversation_id: str
    slug: str
    revision: int
    dataset_version: str | None
    execution_mode: str | None = None


_CURRENT: ContextVar[AnalysisExecutionContext | None] = ContextVar(
    "deterministic_analysis_context", default=None
)
_EXECUTION_RECORD: ContextVar[dict[str, object] | None] = ContextVar(
    "deterministic_execution_record", default=None
)


def current_analysis_context() -> AnalysisExecutionContext | None:
    return _CURRENT.get()


def current_execution_record() -> dict[str, object] | None:
    return _EXECUTION_RECORD.get()


def set_execution_record(record: dict[str, object]) -> None:
    if _CURRENT.get() is None:
        raise RuntimeError(
            "deterministic execution record requires an analysis context"
        )
    _EXECUTION_RECORD.set(record)


@contextmanager
def analysis_execution_context(
    descriptor: dict[str, object] | None,
    conversation_id: str,
    *,
    execution_mode: str | None = None,
) -> Iterator[AnalysisExecutionContext | None]:
    # Direct requests have no workbook catalog entry. Give every direct request a
    # transient slug so the deployment's auto policy can prefer Python for small,
    # supported plans without creating a persisted named analysis. Unsupported
    # plans still remain on the existing SQL executor.
    if descriptor is None:
        descriptor = {"slug": "query", "revision": 1}
    context = AnalysisExecutionContext(
        conversation_id=conversation_id,
        slug=str(descriptor["slug"]),
        revision=int(descriptor["revision"]),
        dataset_version=(
            str(descriptor["dataset_version"])
            if descriptor.get("dataset_version") is not None
            else None
        ),
        execution_mode=execution_mode,
    )
    token = _CURRENT.set(context)
    record_token = _EXECUTION_RECORD.set(None)
    try:
        yield context
    finally:
        _EXECUTION_RECORD.reset(record_token)
        _CURRENT.reset(token)


def enforce_execution_response(response: dict, requested: str | None) -> dict:
    """Report the backend actually used; never label a SQL fallback as Python/parity.

    Called before saving an analysis or emitting its final answer. Clarification
    and failed planning do not count as execution of either backend.
    """
    if (
        response.get("clarify")
        or response.get("error")
        or response.get("result") is None
    ):
        return response
    record = response.get("deterministic")
    actual = record.get("mode") if record else "sql"
    execution = {
        "requested": requested or "default",
        "actual": actual,
        "verified": actual == "verify",
        "implementation": "shared_plan" if record else "sql_executor",
        "fallback_reason": record.get("fallback_reason") if record else None,
    }
    if requested in {"python", "verify"} and actual != requested:
        return {
            "question": response.get("question", ""),
            "error": "This query cannot run with the requested Python execution mode. "
            "Use sql or auto; this query shape is outside the shared-plan subset.",
            "execution": {**execution, "verified": False},
        }
    return {**response, "execution": execution}
