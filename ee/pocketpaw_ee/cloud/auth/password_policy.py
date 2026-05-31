"""Password policy + HIBP k-anonymity breach check.

Exposes :func:`validate_password_async` for the fastapi-users UserManager
to call on both register and password-change paths. Raises
:class:`fastapi_users.exceptions.InvalidPasswordException` with a typed
``reason`` string ("too_short", "missing_uppercase", ..., "breached") so
the frontend can format the message.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time

import httpx
from fastapi_users.exceptions import InvalidPasswordException

logger = logging.getLogger(__name__)

MIN_LENGTH = 12
_HIBP_RANGE_URL = "https://api.pwnedpasswords.com/range/{prefix}"
_HIBP_TIMEOUT = 3.0
_CACHE_TTL_SECONDS = 24 * 60 * 60
# Why: cap at 1000 to bound memory; tiny LRU is plenty for typical traffic.
_CACHE_MAX_ENTRIES = 1000

_UPPERCASE_RE = re.compile(r"[A-Z]")
_LOWERCASE_RE = re.compile(r"[a-z]")
_DIGIT_RE = re.compile(r"\d")
_SYMBOL_RE = re.compile(r"[^A-Za-z0-9]")

_hibp_cache: dict[str, tuple[bool, float]] = {}


def _hibp_enabled() -> bool:
    return os.environ.get("POCKETPAW_HIBP_ENABLED", "true").lower() != "false"


def _cache_get(sha1: str) -> bool | None:
    entry = _hibp_cache.get(sha1)
    if entry is None:
        return None
    is_breached, expires_at = entry
    if expires_at < time.monotonic():
        _hibp_cache.pop(sha1, None)
        return None
    return is_breached


def _cache_put(sha1: str, is_breached: bool) -> None:
    if len(_hibp_cache) >= _CACHE_MAX_ENTRIES:
        # FIFO eviction — dict preserves insertion order.
        oldest = next(iter(_hibp_cache))
        _hibp_cache.pop(oldest, None)
    _hibp_cache[sha1] = (is_breached, time.monotonic() + _CACHE_TTL_SECONDS)


async def _is_breached(password: str) -> bool:
    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    cached = _cache_get(sha1)
    if cached is not None:
        return cached

    prefix, suffix = sha1[:5], sha1[5:]
    try:
        async with httpx.AsyncClient(timeout=_HIBP_TIMEOUT) as client:
            resp = await client.get(
                _HIBP_RANGE_URL.format(prefix=prefix),
                headers={"Add-Padding": "true"},
            )
            resp.raise_for_status()
            body = resp.text
    except (httpx.HTTPError, httpx.TimeoutException) as exc:
        # Why: fail-open on transport error — HIBP downtime must not block
        # password changes. Log and treat as not-breached.
        logger.warning("HIBP check failed (%s); allowing password", exc)
        return False

    is_breached = False
    for line in body.splitlines():
        hash_suffix, _, count_str = line.partition(":")
        if hash_suffix.strip().upper() != suffix:
            continue
        try:
            count = int(count_str.strip())
        except ValueError:
            continue
        if count >= 1:
            is_breached = True
            break

    _cache_put(sha1, is_breached)
    return is_breached


async def validate_password_async(password: str, *, email: str) -> None:
    """Validate ``password`` against the policy. Raises on failure."""
    if len(password) < MIN_LENGTH:
        raise InvalidPasswordException(reason="too_short")
    if not _UPPERCASE_RE.search(password):
        raise InvalidPasswordException(reason="missing_uppercase")
    if not _LOWERCASE_RE.search(password):
        raise InvalidPasswordException(reason="missing_lowercase")
    if not _DIGIT_RE.search(password):
        raise InvalidPasswordException(reason="missing_digit")
    if not _SYMBOL_RE.search(password):
        raise InvalidPasswordException(reason="missing_symbol")

    local_part = email.split("@", 1)[0] if email else ""
    # Substring (not equality): a password like ``Prakash123!`` for the
    # email ``prakash@x.com`` is the same bad practice as ``prakash`` — the
    # exact-match check we had before missed it. Length guard avoids
    # flagging single-character local parts as forbidden.
    if local_part and len(local_part) >= 3 and local_part.lower() in password.lower():
        raise InvalidPasswordException(reason="email_local_part")

    if _hibp_enabled() and await _is_breached(password):
        raise InvalidPasswordException(reason="breached")
