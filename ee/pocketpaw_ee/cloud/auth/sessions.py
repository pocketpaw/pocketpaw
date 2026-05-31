"""Per-user auth-session tracking + revocation.

Persists one ``AuthSession`` row per minted JWT and maintains a Redis
revocation marker per ``(user_id, jti)`` that :class:`RevocableJWTStrategy`
consults on every token read.

Schema is a string key per revoked jti, ``revoked_jti:{user_id}:{jti}``,
with TTL matching the JWT lifetime so each marker auto-expires the
moment the underlying JWT would have stopped being accepted anyway.
Earlier versions used one set per user with a single 7d EXPIRE; that
was renewed on every SADD, which meant stale jtis accumulated past
their actual JWT exp for active accounts.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from fastapi import Request

from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.models.auth_session import AuthSession

logger = logging.getLogger(__name__)

# Must match auth.core.TOKEN_LIFETIME — kept duplicate to avoid circular import.
_REDIS_KEY_TTL = 60 * 60 * 24 * 7  # 7 days


def _revoked_key(user_id: str, jti: str) -> str:
    return f"revoked_jti:{user_id}:{jti}"


def _parse_device_label(user_agent: str | None) -> str:
    if not user_agent:
        return ""
    ua = user_agent
    browsers = ["Edge", "Chrome", "Firefox", "Safari"]
    oses = [
        ("Windows", "Windows"),
        ("Macintosh", "macOS"),
        ("Mac OS X", "macOS"),
        ("Android", "Android"),
        ("iPhone", "iOS"),
        ("iPad", "iOS"),
        ("Linux", "Linux"),
    ]
    browser = next((b for b in browsers if b in ua), "")
    os_label = next((label for needle, label in oses if needle in ua), "")
    if browser and os_label:
        return f"{browser} · {os_label}"
    return browser or os_label


def _trust_forwarded() -> bool:
    """X-Forwarded-For is only honoured when this is set.

    Without a trusted proxy, any client can spoof the header — the IP we
    persist on AuthSession would then be attacker-controlled. Operators
    explicitly opt in by setting ``POCKETPAW_TRUST_FORWARDED_FOR=true``
    when the deploy actually sits behind a reverse proxy that strips
    inbound XFF and appends the real client.
    """
    return os.environ.get("POCKETPAW_TRUST_FORWARDED_FOR", "false").lower() == "true"


def _client_ip(request: Request) -> str | None:
    if _trust_forwarded():
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip() or None
    return request.client.host if request.client else None


async def record_session(user_id: str, jti: str, request: Request) -> AuthSession:
    ua = request.headers.get("user-agent")
    doc = AuthSession(
        user_id=user_id,
        jti=jti,
        ip=_client_ip(request),
        user_agent=ua,
        device_label=_parse_device_label(ua),
    )
    await doc.insert()
    return doc


async def list_sessions(user_id: str) -> list[AuthSession]:
    rows = await AuthSession.find(
        AuthSession.user_id == user_id,
        AuthSession.revoked == False,  # noqa: E712
    ).to_list()
    rows.sort(key=lambda s: s.issued_at, reverse=True)
    return rows


async def _mark_revoked(user_id: str, jti: str) -> None:
    redis = redis_client.get_redis()
    await redis.set(_revoked_key(user_id, jti), "1", ex=_REDIS_KEY_TTL)  # type: ignore[misc]


# Back-compat alias for any external caller.
_add_to_revoked_set = _mark_revoked


async def revoke_session(user_id: str, jti: str, *, by_user_id: str) -> AuthSession:
    doc = await AuthSession.find_one(
        AuthSession.user_id == user_id,
        AuthSession.jti == jti,
    )
    if doc is None:
        raise NotFound("session", jti)
    if not doc.revoked:
        doc.revoked = True
        doc.revoked_at = datetime.now(UTC)
        await doc.save()
    await _mark_revoked(user_id, jti)
    logger.info("revoked session jti=%s user=%s by=%s", jti, user_id, by_user_id)
    return doc


async def revoke_all_others(user_id: str, current_jti: str) -> int:
    rows = await AuthSession.find(
        AuthSession.user_id == user_id,
        AuthSession.revoked == False,  # noqa: E712
    ).to_list()
    now = datetime.now(UTC)
    count = 0
    for row in rows:
        if row.jti == current_jti:
            continue
        row.revoked = True
        row.revoked_at = now
        await row.save()
        await _mark_revoked(user_id, row.jti)
        count += 1
    return count


async def revoke_all_sessions_for_user(user_id: str) -> int:
    """Revoke every active session row for ``user_id`` (no current-jti carve-out).

    Used by the member-removal cascade — force re-login across the whole
    system. Marks each jti in Redis individually so per-jti TTLs apply.
    Returns the count of newly revoked rows.
    """
    rows = await AuthSession.find(
        AuthSession.user_id == user_id,
        AuthSession.revoked == False,  # noqa: E712
    ).to_list()
    if not rows:
        return 0
    now = datetime.now(UTC)
    jtis: list[str] = []
    for row in rows:
        row.revoked = True
        row.revoked_at = now
        await row.save()
        jtis.append(row.jti)
    for jti in jtis:
        try:
            await _mark_revoked(user_id, jti)
        except Exception as exc:  # noqa: BLE001
            logger.warning("revoke_all_sessions_for_user Redis update failed for %s: %s", jti, exc)
    return len(jtis)


async def is_revoked(user_id: str, jti: str) -> bool:
    """True if ``jti`` is in the revocation list — Redis primary, Mongo backstop.

    Why a backstop: Redis is the fast path (every authenticated call
    pays one SISMEMBER) but losing it must not silently un-revoke every
    kicked / logged-out / password-reset session. On Redis error we fall
    back to the durable ``AuthSession.revoked`` row so revocation still
    holds; the cost is one Mongo round-trip during a Redis outage.
    """
    # TODO: cache per-request via contextvar; Redis SISMEMBER round-trip is
    # fine for now but every authenticated call pays it.
    try:
        redis = redis_client.get_redis()
        return bool(await redis.exists(_revoked_key(user_id, jti)))  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001
        logger.warning("is_revoked Redis check failed; falling back to Mongo: %s", exc)
    try:
        doc = await AuthSession.find_one(
            AuthSession.user_id == user_id,
            AuthSession.jti == jti,
        )
    except Exception as exc:  # noqa: BLE001
        # Mongo down too — true fail-closed would lock everyone out. Keep
        # the request flowing but make the failure loud.
        logger.error("is_revoked Mongo backstop also failed: %s", exc)
        return False
    return bool(doc and doc.revoked)


async def touch_session(user_id: str, jti: str) -> None:
    try:
        doc = await AuthSession.find_one(
            AuthSession.user_id == user_id,
            AuthSession.jti == jti,
        )
        if doc is None:
            return
        doc.last_seen_at = datetime.now(UTC)
        await doc.save()
    except Exception as exc:  # noqa: BLE001
        logger.debug("touch_session best-effort failure: %s", exc)


__all__ = [
    "is_revoked",
    "list_sessions",
    "record_session",
    "revoke_all_others",
    "revoke_all_sessions_for_user",
    "revoke_session",
    "touch_session",
]
