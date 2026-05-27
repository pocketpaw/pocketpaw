"""Short-lived single-use JWT for WebSocket upgrade authentication.

Why this exists: Chrome's `SameSite=Lax` excludes cookies from
script-initiated cross-origin WebSocket upgrades (RFC 6265bis §8.8.1).
On a cross-subdomain SPA→backend deploy that means `paw_auth` never
reaches the WS handshake. We refuse to flip the session cookie to
`SameSite=None` (broader CSRF surface) and refuse to surface the
long-lived JWT to JS (XSS regression). The standard production pattern
is a short-lived ticket: the client uses its existing REST session to
mint one, then passes it as `?token=` on the upgrade. The ticket is
single-use (consumed atomically via Redis DEL) and expires in seconds,
so URL/log exposure of the value is inert.

Token shape: same HS256 + SECRET as the session JWT, but with
``aud=["ws"]`` and ``type=ws_ticket``. The chat WS handler decodes with
that audience and DELs the jti from Redis; if the DEL returns 0, the
ticket was never minted or has already been consumed.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

import jwt

from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud.auth.core import SECRET

logger = logging.getLogger(__name__)

_TOKEN_TYPE = "ws_ticket"
_AUDIENCE = ["ws"]
_LIFETIME_SECONDS = 30
_ALGORITHM = "HS256"
_REDIS_KEY_PREFIX = "ws_ticket:"


def _redis_key(jti: str) -> str:
    return f"{_REDIS_KEY_PREFIX}{jti}"


async def mint_ws_ticket(user_id: str) -> str:
    """Mint a single-use ticket bound to ``user_id``.

    Registers the jti in Redis with a TTL matching the JWT expiry so an
    unconsumed ticket falls off on its own. The consume path DELs the
    key; second consume returns 0 → invalid.
    """
    jti = secrets.token_urlsafe(16)
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": user_id,
        "type": _TOKEN_TYPE,
        "aud": _AUDIENCE,
        "jti": jti,
        "iat": now,
        "exp": now + _LIFETIME_SECONDS,
    }
    token = jwt.encode(payload, SECRET, algorithm=_ALGORITHM)

    redis = redis_client.get_redis()
    await redis.set(_redis_key(jti), user_id, ex=_LIFETIME_SECONDS)  # type: ignore[misc]
    return token


async def consume_ws_ticket(token: str) -> str | None:
    """Validate ``token`` and atomically consume it. Returns the bound
    user_id on success, ``None`` on any failure (invalid signature, wrong
    audience, expired, never minted, or already consumed).
    """
    try:
        payload = jwt.decode(token, SECRET, algorithms=[_ALGORITHM], audience=_AUDIENCE)
    except jwt.PyJWTError:
        return None
    if payload.get("type") != _TOKEN_TYPE:
        return None
    user_id = payload.get("sub")
    jti = payload.get("jti")
    if not isinstance(user_id, str) or not isinstance(jti, str):
        return None

    try:
        redis = redis_client.get_redis()
        deleted = await redis.delete(_redis_key(jti))  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001
        # Why: a Redis outage shouldn't silently grant access. Fail closed.
        logger.warning("ws_ticket Redis consume failed: %s", exc)
        return None
    if not deleted:
        return None
    return user_id


__all__ = ["consume_ws_ticket", "mint_ws_ticket"]
