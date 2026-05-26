"""Per-user auth-session tracking + revocation.

Persists one ``AuthSession`` row per minted JWT and maintains a Redis
``revoked_jti:{user_id}`` set the :class:`RevocableJWTStrategy` consults
on every token read.

Redis set entries get a TTL roughly matching JWT lifetime so the set
auto-trims (a revoked entry past the JWT exp is no longer reachable).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import Request

from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.models.auth_session import AuthSession

logger = logging.getLogger(__name__)

# Must match auth.core.TOKEN_LIFETIME — kept duplicate to avoid circular import.
_REDIS_SET_TTL = 60 * 60 * 24 * 7  # 7 days


def _revoked_key(user_id: str) -> str:
    return f"revoked_jti:{user_id}"


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


def _client_ip(request: Request) -> str | None:
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


async def _add_to_revoked_set(user_id: str, jti: str) -> None:
    redis = redis_client.get_redis()
    key = _revoked_key(user_id)
    await redis.sadd(key, jti)  # type: ignore[misc]
    await redis.expire(key, _REDIS_SET_TTL)  # type: ignore[misc]


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
    await _add_to_revoked_set(user_id, jti)
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
        await _add_to_revoked_set(user_id, row.jti)
        count += 1
    return count


async def is_revoked(user_id: str, jti: str) -> bool:
    # TODO: cache per-request via contextvar; Redis SISMEMBER round-trip is
    # fine for now but every authenticated call pays it.
    try:
        redis = redis_client.get_redis()
        return bool(await redis.sismember(_revoked_key(user_id), jti))  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001
        logger.warning("is_revoked Redis check failed (fail-open): %s", exc)
        return False


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
    "revoke_session",
    "touch_session",
]
