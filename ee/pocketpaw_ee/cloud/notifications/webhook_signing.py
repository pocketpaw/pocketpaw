# ee/pocketpaw_ee/cloud/notifications/webhook_signing.py
# Signing for outbound notification webhooks (the workspace webhook and each
# site's lead webhook). Pure functions, no I/O, so a receiver's verification
# recipe in docs/api-reference.md can be checked against this file line by line.
#
# Wire contract:
#   X-Paw-Timestamp: <unix seconds>
#   X-Paw-Signature: v1=<hex HMAC-SHA256(secret, f"{timestamp}.{raw_body}")>
#   body: {"id", "type", "created_at", "data"}
#
# During a secret rotation's grace window the header carries two values,
# ``v1=<new>,v1=<old>``; a receiver accepts the delivery if either verifies.
# ``id`` is stable across retries of one delivery, so a receiver can dedupe.
# The ``v1=`` prefix leaves room to rotate the scheme without breaking parsers.
# ``verify_signature`` is what a Python receiver runs; it also rejects a
# timestamp outside ``tolerance_seconds`` so a captured delivery can't be
# replayed later.

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Sequence
from typing import Any

TIMESTAMP_HEADER = "X-Paw-Timestamp"
SIGNATURE_HEADER = "X-Paw-Signature"
SIGNATURE_VERSION = "v1"
DEFAULT_TOLERANCE_SECONDS = 300


def compute_signature(secret: str, timestamp: str, body: str | bytes) -> str:
    """Hex HMAC-SHA256 of ``f"{timestamp}.{body}"`` keyed by ``secret``."""
    raw = body.encode("utf-8") if isinstance(body, str) else body
    message = timestamp.encode("utf-8") + b"." + raw
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def signature_header(secret: str, timestamp: str, body: str | bytes) -> str:
    return f"{SIGNATURE_VERSION}={compute_signature(secret, timestamp, body)}"


def sign_headers(
    secrets: str | Sequence[str], body: str, *, now: float | None = None
) -> dict[str, str]:
    """Headers for one delivery. Several secrets (the new one first, then the
    one it replaced, during a rotation's grace window) give several
    comma-separated ``v1=`` values; a receiver accepts any that verifies."""
    timestamp = str(int(now if now is not None else time.time()))
    keys = [secrets] if isinstance(secrets, str) else [k for k in secrets if k]
    return {
        "Content-Type": "application/json",
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: ",".join(signature_header(k, timestamp, body) for k in keys),
    }


def verify_signature(
    secret: str,
    timestamp: str,
    body: str | bytes,
    header: str,
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: float | None = None,
) -> bool:
    """True when ``header`` carries a valid v1 signature for a fresh timestamp."""
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = now if now is not None else time.time()
    if abs(current - sent_at) > tolerance_seconds:
        return False
    expected = compute_signature(secret, timestamp, body)
    for part in (header or "").split(","):
        version, _, value = part.strip().partition("=")
        if version == SIGNATURE_VERSION and hmac.compare_digest(value, expected):
            return True
    return False


def build_event(*, event_id: str, event_type: str, created_at: str, data: Any) -> dict[str, Any]:
    return {"id": event_id, "type": event_type, "created_at": created_at, "data": data}


def encode_event(event: dict[str, Any]) -> str:
    """The exact body string that is signed and sent."""
    return json.dumps(event, separators=(",", ":"), default=str)


__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TIMESTAMP_HEADER",
    "build_event",
    "compute_signature",
    "encode_event",
    "sign_headers",
    "signature_header",
    "verify_signature",
]
