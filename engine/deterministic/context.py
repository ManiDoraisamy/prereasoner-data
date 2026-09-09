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
        raise RuntimeError("deterministic execution record requires an analysis context")
    _EXECUTION_RECORD.set(record)


@contextmanager
def analysis_execution_context(
    descriptor: dict[str, object] | None, conversation_id: str
) -> Iterator[AnalysisExecutionContext | None]:
    if descriptor is None:
        yield None
        return
    context = AnalysisExecutionContext(
        conversation_id=conversation_id,
        slug=str(descriptor["slug"]),
        revision=int(descriptor["revision"]),
        dataset_version=(
            str(descriptor["dataset_version"])
            if descriptor.get("dataset_version") is not None
            else None
        ),
    )
    token = _CURRENT.set(context)
    record_token = _EXECUTION_RECORD.set(None)
    try:
        yield context
    finally:
        _EXECUTION_RECORD.reset(record_token)
        _CURRENT.reset(token)
