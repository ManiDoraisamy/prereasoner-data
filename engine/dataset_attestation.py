"""Authenticated transport for orchestrator-emitted dataset-semantics operations.

The browser-facing engine accepts ordinary data questions directly, but only the orchestrator is
allowed to mint conversation claims. Both services share one deployment secret and sign the exact
operation list together with the authenticated user's stable principal. The stored operation keeps
only the server-generated ``attested`` marker; the transport signature never enters conversation
state or the model context.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os


HEADER = "X-Prereasoner-Dataset-Attestation"
_VERSION = "v1"


def _key() -> bytes | None:
    value = os.environ.get("DATASET_ATTESTATION_KEY", "")
    if value:
        return value.encode("utf-8")
    if os.environ.get("APP_ENV", "production").strip().lower() in {"development", "test"}:
        return b"prereasoner-local-dataset-attestation"
    return None


def _payload(principal: str, ops) -> bytes:
    return json.dumps(
        {"principal": str(principal), "ops": ops},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def configured() -> bool:
    """Whether this runtime can sign or verify claims under its environment policy."""
    return _key() is not None


def sign(principal: str | None, ops) -> str | None:
    """Return a versioned signature, or ``None`` when signing is unavailable."""
    key = _key()
    if key is None or not principal or not isinstance(ops, list) or not ops:
        return None
    digest = hmac.new(key, _payload(principal, ops), hashlib.sha256).hexdigest()
    return f"{_VERSION}={digest}"


def verify(principal: str | None, ops, signature: str | None) -> bool:
    """Verify the exact principal/operation payload with constant-time comparison."""
    expected = sign(principal, ops)
    return bool(expected and signature and hmac.compare_digest(expected, str(signature)))


def verify_quotes(raw_ops, user_message, history=None):
    """Return normalized ops and whether every quote is present in the current user message.

    The engine remains the authority for grammar and table binding. This boundary owns provenance:
    a model must not be able to manufacture a quote that the UI later presents as "you said".
    Historical messages are deliberately excluded — the unused ``history`` parameter documents that
    contract (an old statement cannot authenticate a newly emitted operation). Invalid quotes are
    sent without an attestation so the engine returns a deterministic clarify instead of silently
    accepting or dropping the correction.
    """
    if not isinstance(raw_ops, list):
        return raw_ops, False
    user_text = str(user_message or "")
    verified = bool(raw_ops)
    out = []
    for raw in raw_ops:
        op = dict(raw) if isinstance(raw, dict) else raw
        if not isinstance(op, dict):
            verified = False
            out.append(op)
            continue
        basis = op.get("basis")
        if not isinstance(basis, dict):
            verified = False
            out.append(op)
            continue
        quoted = str(basis.get("text", "")).strip()
        if (basis.get("source") != "conversation" or not quoted
                or quoted.casefold() not in user_text.casefold()):
            verified = False
        op["basis"] = {"source": "conversation", "text": quoted}
        out.append(op)
    return out, verified
