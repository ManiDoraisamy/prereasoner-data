"""Named analysis identity and derivation-view naming.

An analysis is a user-visible workbook over the conversation's shared input tables.  The
orchestrator proposes whether a question creates, modifies, or inspects an analysis; the
engine validates that proposal and owns the durable identity.  SQL planning remains wholly
outside this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any

ANALYSIS_ACTIONS = frozenset({"create", "modify", "inspect"})
MAX_ANALYSIS_SLUG_BYTES = 40
MAX_ANALYSIS_SNAPSHOT_BYTES = 1 * 1024 * 1024
_ANALYSIS_ID = re.compile(r"^a_[0-9a-f]{32}$")
_SLUG = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


class AnalysisError(ValueError):
    """A named-analysis request is malformed, stale, or unauthorized."""


class AnalysisConflict(AnalysisError):
    """The dataset or revision changed after this request began."""


def canonical_analysis_slug(value: object) -> str:
    """Return a short PostgreSQL-safe slug proposed by the conversational model."""
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not slug:
        slug = "analysis"
    if not slug[0].isalpha():
        slug = "analysis_" + slug
    if len(slug.encode("ascii")) > MAX_ANALYSIS_SLUG_BYTES:
        digest = hashlib.sha256(slug.encode("ascii")).hexdigest()[:8]
        slug = slug[:MAX_ANALYSIS_SLUG_BYTES - len(digest) - 1].rstrip("_") + "_" + digest
    if not _SLUG.fullmatch(slug):
        raise AnalysisError("analysis slug is invalid")
    return slug


def validate_analysis_spec(value: object) -> dict[str, Any] | None:
    """Validate the transport shape; conversation ownership is checked by persistence."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AnalysisError("analysis must be an object")
    unknown = set(value) - {"action", "analysis_id", "slug", "revision"}
    if unknown:
        raise AnalysisError(f"analysis contains unsupported fields: {', '.join(sorted(unknown))}")
    action = value.get("action")
    if action not in ANALYSIS_ACTIONS:
        raise AnalysisError("analysis action must be create, modify, or inspect")
    raw_slug = value.get("slug")
    if not isinstance(raw_slug, str) or not raw_slug.strip():
        raise AnalysisError("analysis slug is required")
    slug = canonical_analysis_slug(raw_slug)
    analysis_id = value.get("analysis_id")
    if analysis_id not in (None, ""):
        if not isinstance(analysis_id, str) or not _ANALYSIS_ID.fullmatch(analysis_id):
            raise AnalysisError("analysis_id is invalid")
    else:
        analysis_id = None
    revision = value.get("revision")
    if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool)
                                 or revision < 1 or revision > 1_000_000):
        raise AnalysisError("analysis revision is invalid")
    if action == "create" and analysis_id is not None:
        raise AnalysisError("create must not name an existing analysis_id")
    if action in {"modify", "inspect"} and analysis_id is None:
        raise AnalysisError(f"{action} requires analysis_id")
    if action != "inspect" and revision is not None:
        raise AnalysisError("only inspect may select a revision")
    return {"action": action, "analysis_id": analysis_id, "slug": slug, "revision": revision}


def analysis_display_name(slug: str) -> str:
    return str(slug or "analysis").replace("_", " ")


def analysis_view_name(slug: str, logical_name: object) -> str:
    """Prefix one engine-owned logical view name without exceeding PostgreSQL's 63 bytes."""
    suffix = re.sub(r"[^a-z0-9_]+", "_", str(logical_name or "step").lower()).strip("_") or "step"
    name = f"{canonical_analysis_slug(slug)}_{suffix}"
    if len(name.encode("ascii")) <= 63:
        return name
    digest = hashlib.sha256(name.encode("ascii")).hexdigest()[:8]
    return name[:63 - len(digest) - 1].rstrip("_") + "_" + digest


def analysis_input_hash(tables, *, explicit_fks=(), dataset_semantics=()) -> str:
    """Fingerprint the effective tables and structural metadata used by planning."""
    material = []
    for table in tables or ():
        if not isinstance(table, dict):
            continue
        material.append({
            "name": str(table.get("name") or ""),
            "columns": list(table.get("columns") or ()),
            "rows": list(table.get("rows") or ()),
        })
    material.sort(key=lambda table: table["name"])
    encoded = json.dumps({
        "tables": material,
        "explicit_fks": sorted(
            (dict(edge) for edge in (explicit_fks or ()) if isinstance(edge, dict)),
            key=lambda edge: json.dumps(edge, sort_keys=True, default=str),
        ),
        "dataset_semantics": list(dataset_semantics or ()),
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _unique_view_name(slug: str, logical: str, used: set[str]) -> str:
    base = analysis_view_name(slug, logical)
    candidate = base
    suffix = 2
    while candidate in used:
        tail = f"_{suffix}"
        candidate = base[:63 - len(tail)].rstrip("_") + tail
        suffix += 1
    used.add(candidate)
    return candidate


def _decorate_view(view: dict[str, Any], slug: str, used: set[str]) -> dict[str, Any]:
    item = dict(view)
    logical = str(item.get("name") or item.get("op") or "step")
    prefix = canonical_analysis_slug(slug) + "_"
    if logical.startswith(prefix) and logical not in used:
        prefixed = logical
        used.add(prefixed)
        logical_name = str(item.get("logical_name") or logical[len(prefix):])
    else:
        prefixed = _unique_view_name(slug, logical, used)
        logical_name = logical
    item["logical_name"] = logical_name
    item["name"] = prefixed
    return item


def decorate_analysis_response(response: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    """Attach analysis identity and prefix one response's complete derivation stack."""
    out = dict(response)
    descriptor = dict(analysis)
    descriptor["display_name"] = analysis_display_name(descriptor["slug"])
    out["analysis"] = descriptor
    used: set[str] = set()
    source_views = [view for view in (out.get("views") or ()) if isinstance(view, dict)]
    if not source_views and isinstance(out.get("result"), dict):
        result = out["result"]
        source_views = [{
            "name": "result",
            "op": "select",
            "label": "result",
            "sql": out.get("sql") or "",
            "columns": list(result.get("columns") or ()),
            "rows": list(result.get("rows") or ()),
            "column_provenance": list(result.get("column_provenance") or ()),
        }]
    views = [_decorate_view(view, descriptor["slug"], used) for view in source_views]
    out["views"] = views
    return out


def analysis_emitter(emit: Callable[..., Any], analysis: dict[str, Any]):
    """Decorate live view events exactly as the eventual HTTP response is decorated."""
    used: set[str] = set()
    descriptor = dict(analysis)
    descriptor["display_name"] = analysis_display_name(descriptor["slug"])

    def wrapped(node, value, merge=False):
        if node == "analysis":
            update = value if isinstance(value, dict) else {}
            descriptor.update(update)
            return emit(node, dict(descriptor), merge)
        if isinstance(node, str) and node.startswith("views/") and isinstance(value, dict):
            value = _decorate_view(value, descriptor["slug"], used)
        return emit(node, value, merge)

    return wrapped
