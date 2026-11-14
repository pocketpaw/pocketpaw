# ee/pocketpaw_ee/sites/figma/session_keys.py: which Figma files a conversation may read.
#
# The Figma tools read only file keys the user pasted into THIS chat session
# themselves. ``note_message`` runs in the chat run path (``run_core``) on every
# /sites turn with the user's own text, BEFORE the agent sees it, and records each
# design link's file key under (workspace, session, user). ``allowed`` is the check
# every tool makes before any Figma request. A key the agent read somewhere else (a
# layer name, a web page, another chat) is refused, so a prompt injection cannot
# steer the tools at a file the owner never offered.
#
# Storage is a Redis set per (workspace, session, user), ``TTL_SECONDS`` sliding,
# at most ``MAX_KEYS`` keys. Without Redis (tests, a local single process) it falls
# back to an in-process dict with the same shape. No session id means no allowlist:
# ``allowed`` answers False (fail closed).

from __future__ import annotations

import logging
import time

from pocketpaw_ee.sites.figma.urls import find_links

logger = logging.getLogger(__name__)

TTL_SECONDS = 30 * 24 * 3600
MAX_KEYS = 20
_PREFIX = "figma:keys"
_memory: dict[str, tuple[float, set[str]]] = {}


def _key(workspace_id: str, session_id: str, user_id: str) -> str:
    return f"{_PREFIX}:{workspace_id}:{session_id}:{user_id}"


def _redis():
    try:
        from pocketpaw_ee.cloud._core.redis_client import get_redis

        return get_redis()
    except Exception:  # noqa: BLE001 - no Redis configured: the in-process fallback
        return None


def clear_memory() -> None:
    _memory.clear()


async def add(workspace_id: str, session_id: str, user_id: str, keys: list[str]) -> None:
    if not (workspace_id and session_id and user_id and keys):
        return
    name = _key(workspace_id, session_id, user_id)
    client = _redis()
    if client is not None:
        try:
            current = await client.scard(name)
            fresh = [k for k in keys if not await client.sismember(name, k)]
            room = max(0, MAX_KEYS - int(current or 0))
            if fresh[:room]:
                await client.sadd(name, *fresh[:room])
            await client.expire(name, TTL_SECONDS)
            return
        except Exception:  # noqa: BLE001
            logger.warning("figma: session allowlist write failed; using memory", exc_info=True)
    now = time.monotonic()
    until, held = _memory.get(name, (0.0, set()))
    if until <= now:
        held = set()
    for k in keys:
        if len(held) >= MAX_KEYS:
            break
        held.add(k)
    _memory[name] = (now + TTL_SECONDS, held)


async def note_message(
    workspace_id: str, session_id: str | None, user_id: str, text: str
) -> list[str]:
    """Record the file keys of every Figma design link in the user's own message."""
    keys = list(dict.fromkeys(ref.file_key for ref in find_links(text)))
    if keys and session_id:
        await add(workspace_id, session_id, user_id, keys)
    return keys


async def allowed(workspace_id: str, session_id: str | None, user_id: str, key: str) -> bool:
    if not (workspace_id and session_id and user_id and key):
        return False
    name = _key(workspace_id, session_id, user_id)
    client = _redis()
    if client is not None:
        try:
            return bool(await client.sismember(name, key))
        except Exception:  # noqa: BLE001
            logger.warning("figma: session allowlist read failed; using memory", exc_info=True)
    until, held = _memory.get(name, (0.0, set()))
    return until > time.monotonic() and key in held
