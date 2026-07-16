# state.py — signed OAuth ``state`` for the Code Mode GitHub connect flow (CM-3).
# Created 2026-07-16 (feat/code-mode): the GitHub App install redirect comes back
# as a top-level BROWSER navigation to our callback — it carries no bearer token,
# so the callback can't use the normal RequestContext auth. Instead the install-URL
# step signs the caller's ``(workspace_id, user_id)`` into a short-lived, tamper-
# proof ``state`` token; the callback verifies it to recover who is connecting.
#
# Reuses the exact primitive the ws_tickets use — PyJWT HS256 over the shared
# ``auth.core.SECRET`` — so there's no new secret and no new dependency. Stateless
# (no Redis): the token is self-describing and expiring, and the worst a replay can
# do is re-bind the SAME (already authenticated) user to the SAME installation.

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import jwt

from pocketpaw_ee.cloud.auth.core import SECRET

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"
_AUDIENCE = "code-github-connect"
# The install round-trip (click → GitHub install screen → callback) is
# interactive; 15 minutes is comfortably longer than a human takes and short
# enough that a leaked state is useless well before it could be abused.
_LIFETIME_SECONDS = 900


def sign_state(workspace_id: str, user_id: str, *, now: datetime | None = None) -> str:
    """Sign ``(workspace_id, user_id)`` into a short-lived install ``state`` token."""
    reference = now or datetime.now(UTC)
    payload = {
        "ws": workspace_id,
        "sub": user_id,
        "aud": _AUDIENCE,
        "iat": int(reference.timestamp()),
        "exp": int((reference + timedelta(seconds=_LIFETIME_SECONDS)).timestamp()),
    }
    return jwt.encode(payload, SECRET, algorithm=_ALGORITHM)


def verify_state(state: str) -> tuple[str, str] | None:
    """Verify an install ``state`` token; return ``(workspace_id, user_id)`` or None.

    Returns None on ANY failure — bad signature, wrong audience, expired, malformed,
    or missing claims — so the callback treats every unverifiable state identically
    (a clean reject, never a distinguishable error shape).
    """
    if not state:
        return None
    try:
        payload = jwt.decode(state, SECRET, algorithms=[_ALGORITHM], audience=_AUDIENCE)
    except Exception:  # noqa: BLE001 — any decode failure is a uniform reject
        logger.debug("code-connect: rejected install state", exc_info=True)
        return None
    workspace_id = payload.get("ws")
    user_id = payload.get("sub")
    if not isinstance(workspace_id, str) or not isinstance(user_id, str):
        return None
    if not workspace_id or not user_id:
        return None
    return workspace_id, user_id


__all__ = ["sign_state", "verify_state"]
