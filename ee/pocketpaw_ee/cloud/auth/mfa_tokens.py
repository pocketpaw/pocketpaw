"""Short-lived JWT for the MFA challenge handshake.

The /auth/login response stops minting the real session JWT once a user
has MFA enabled. Instead it returns one of these tokens — opaque to the
client, decoded server-side at /auth/mfa/challenge to recover the user
the password gate already cleared.

The token carries ``type: mfa_pending`` so it can't be substituted for a
real auth JWT (the JWTStrategy uses a different audience and no type
claim). ``jti`` is exposed for the per-attempt rate limiter key.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

import jwt

from pocketpaw_ee.cloud.auth.core import SECRET

_TOKEN_TYPE = "mfa_pending"
_LIFETIME_SECONDS = 300  # 5 min
_ALGORITHM = "HS256"


def _derived_secret() -> str:
    """Domain-separate from the auth-JWT signing key.

    Why: today the auth JWTStrategy uses a different audience, so a
    forged token with the same SECRET still fails verification. But
    sharing the key means any future strategy change (or accidental
    audience drift) could let one token type substitute for the other.
    Deriving via HMAC-SHA256 keeps the per-bucket entropy of SECRET
    while making cross-bucket substitution structurally impossible.
    """
    return hashlib.sha256(SECRET.encode("utf-8") + b"|mfa-pending|v1").hexdigest()


def mint_mfa_pending(user_id: str) -> tuple[str, str]:
    """Return (token, jti). The jti keys the per-challenge rate limiter."""
    jti = secrets.token_urlsafe(16)
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": user_id,
        "type": _TOKEN_TYPE,
        "jti": jti,
        "iat": now,
        "exp": now + _LIFETIME_SECONDS,
    }
    token = jwt.encode(payload, _derived_secret(), algorithm=_ALGORITHM)
    return token, jti


def verify_mfa_pending(token: str) -> tuple[str, str] | None:
    """Return (user_id, jti) on success, None on invalid/expired/wrong-type."""
    try:
        payload = jwt.decode(token, _derived_secret(), algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != _TOKEN_TYPE:
        return None
    user_id = payload.get("sub")
    jti = payload.get("jti")
    if not isinstance(user_id, str) or not isinstance(jti, str):
        return None
    return user_id, jti


__all__ = ["mint_mfa_pending", "verify_mfa_pending"]
