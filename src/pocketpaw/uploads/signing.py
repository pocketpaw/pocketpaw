"""HMAC-signed short-lived grant tokens for upload access.

A grant binds ``(file_id, expires_unix)`` with an HMAC signature. Clients mint
grants via an authenticated ``GET /uploads/{id}/grant`` endpoint, then open
the bytes via ``GET /uploads/{id}?t={token}`` — no Authorization header
required. That makes signed URLs usable from raw ``<img src>`` / ``<a href
download>`` attributes, which can't carry a Bearer token.

Token format on the wire: ``{expires_unix}.{hex_hmac}``.
Signed message: ``{file_id}:{expires_unix}``.

The caller supplies the signing secret so OSS can use the master access token
and EE can use its JWT signing secret without coupling this module to either.
"""

from __future__ import annotations

import hashlib
import hmac
import time

__all__ = ["DEFAULT_TTL_SECONDS", "sign_grant", "verify_grant"]

DEFAULT_TTL_SECONDS = 300  # 5 minutes — balance between replay risk and UX


def sign_grant(
    file_id: str,
    secret: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> tuple[str, int]:
    """Mint a grant token for ``file_id``. Returns ``(token, expires_unix)``."""
    expires = int(time.time()) + int(ttl_seconds)
    sig = _sign(secret, f"{file_id}:{expires}")
    return f"{expires}.{sig}", expires


def verify_grant(file_id: str, token: str, secret: str) -> bool:
    """Return True if ``token`` is a live grant for ``file_id``."""
    if not token or "." not in token:
        return False
    expires_str, sig = token.split(".", 1)
    try:
        expires = int(expires_str)
    except ValueError:
        return False
    if time.time() > expires:
        return False
    expected = _sign(secret, f"{file_id}:{expires}")
    return hmac.compare_digest(sig, expected)


def _sign(secret: str, message: str) -> str:
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
