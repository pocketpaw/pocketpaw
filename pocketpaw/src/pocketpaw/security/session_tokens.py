"""HMAC-based stateless session tokens with TTL.

Token format: ``{expires_unix}:{hex_hmac}``

The master access token is used as the HMAC key so that regenerating the
master token instantly invalidates all outstanding session tokens — no
server-side session store required.
"""

import hashlib
import hmac
import time

__all__ = ["create_session_token", "verify_session_token"]


def create_session_token(master_token: str, ttl_hours: int = 24) -> str:
    """Issue a session token that expires after *ttl_hours*.

    Returns a string of the form ``{expires_unix}:{hex_hmac}``.
    """
    expires = int(time.time()) + ttl_hours * 3600
    sig = _sign(master_token, str(expires))
    return f"{expires}:{sig}"


def verify_session_token(token: str, master_token: str) -> bool:
    """Verify a session token. Returns True if valid and not expired."""
    parts = token.split(":", 1)
    if len(parts) != 2:
        return False

    expires_str, sig = parts
    try:
        expires = int(expires_str)
    except ValueError:
        return False

    if time.time() > expires:
        return False

    expected = _sign(master_token, expires_str)
    return hmac.compare_digest(sig, expected)


def _sign(key: str, message: str) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
