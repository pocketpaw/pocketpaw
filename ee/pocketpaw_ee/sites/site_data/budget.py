# ee/pocketpaw_ee/sites/site_data/budget.py — the Cloudflare call limiter for the
# Sites Data dashboard.
#
# Cloudflare allows 1,200 API requests per 5 minutes per token, and going over
# blocks the token for 5 minutes. Every dashboard call to Cloudflare (D1, KV, R2)
# spends one token here first, from three fixed-window buckets:
#   * ``cf:data:global``     every call, 900 / 5 min (headroom under the 1,200 cap)
#   * ``cf:data:r2``         R2 calls on top of global, 900 / 5 min
#   * ``cf:data:ws:<id>``    every call from one workspace, 120 / min
# A call is granted only when every bucket it touches has room, and then all of them
# are charged; a refused call charges nothing. Refusal raises ``DataBusy`` (429
# ``sites.data_busy`` with ``details.retry_after`` in seconds).
#
# The buckets live in Redis (the shared ``get_redis()`` client, one Lua script so
# check-and-charge is atomic across processes) when ``POCKETPAW_REDIS_URL`` is set,
# and in this process otherwise. A Redis error falls back to the in-process buckets
# rather than failing the read. ``Budgeted`` wraps a D1 client so each API call (a
# whole batch counts once, and so does each Time Travel call) is charged before it
# is made.
#
# ``acquire_restore`` is a separate, stricter cap on the same machinery: at most 3
# Time Travel restores per database per 10 minutes (Cloudflare allows 10), bucket
# ``cf:data:restore:<database id>``; refusal is 429 ``sites.data_restore_busy``.
# ``claim_once`` / ``release`` are one-shot claims on the same client (Redis
# ``SET NX`` with a TTL, else this process): undoing a restore, running a job.
from __future__ import annotations

import logging
import math
import time
from typing import Any

from pocketpaw_ee.cloud._core.errors import RateLimited

logger = logging.getLogger(__name__)

# (limit, window seconds). Read at call time, so tests and operators can tune them.
GLOBAL_LIMIT: tuple[int, int] = (900, 300)
R2_LIMIT: tuple[int, int] = (900, 300)
WORKSPACE_LIMIT: tuple[int, int] = (120, 60)
RESTORE_LIMIT: tuple[int, int] = (3, 600)

KINDS = frozenset({"d1", "kv", "r2"})

# KEYS: the bucket keys. ARGV: limit, window_ms per key, in KEYS order. Returns {0, 0}
# when granted (every key charged), else {milliseconds until the full bucket resets,
# its 1-based position in KEYS}.
_ACQUIRE_LUA = """
for i = 1, #KEYS do
  local used = tonumber(redis.call('GET', KEYS[i]) or '0')
  if used >= tonumber(ARGV[2 * i - 1]) then
    local ttl = redis.call('PTTL', KEYS[i])
    if ttl < 1 then ttl = tonumber(ARGV[2 * i]) end
    return {ttl, i}
  end
end
for i = 1, #KEYS do
  local n = redis.call('INCR', KEYS[i])
  if n == 1 or redis.call('PTTL', KEYS[i]) < 0 then
    redis.call('PEXPIRE', KEYS[i], ARGV[2 * i])
  end
end
return {0, 0}
"""

_redis: Any | None = None
# key -> [used, window resets at (monotonic seconds)]
_local: dict[str, list[float]] = {}


class DataBusy(RateLimited):
    """The dashboard's Cloudflare budget is spent for now (429). ``scope`` names the
    bucket that was full: ``workspace`` (this workspace's own minute), ``global`` or
    ``r2`` (shared by everyone)."""

    def __init__(self, retry_after: int, scope: str = "workspace") -> None:
        super().__init__(
            "sites.data_busy",
            f"Your site's data is busy. Try again in {retry_after} s.",
        )
        self.details = {"retry_after": retry_after}
        self.scope = scope


def use_redis(client: Any | None) -> None:
    """Share the buckets through ``client`` (None goes back to the default choice)."""
    global _redis
    _redis = client


def reset() -> None:
    """Forget every in-process bucket and claim and any injected Redis (tests)."""
    global _redis
    _redis = None
    _local.clear()
    _claims.clear()


def _buckets(kind: str, workspace_id: str) -> list[tuple[str, int, int]]:
    if kind not in KINDS:
        raise ValueError(f"unknown Cloudflare call kind: {kind!r}")
    out = [("cf:data:global", *GLOBAL_LIMIT)]
    if kind == "r2":
        out.append(("cf:data:r2", *R2_LIMIT))
    out.append((f"cf:data:ws:{workspace_id or '-'}", *WORKSPACE_LIMIT))
    return out


def redis_client() -> Any | None:
    """The Redis client the dashboard shares state through (an injected one first),
    or None when Redis isn't configured."""
    if _redis is not None:
        return _redis
    from pocketpaw_ee.cloud._core.redis_client import get_redis

    try:
        return get_redis()
    except RuntimeError:  # POCKETPAW_REDIS_URL unset: in-process buckets
        return None


def _acquire_local(buckets: list[tuple[str, int, int]]) -> tuple[float, str]:
    """``(0, "")`` when granted, else (seconds until the full bucket resets, its key)."""
    now = time.monotonic()
    for key, limit, window in buckets:
        slot = _local.get(key)
        if slot is not None and slot[1] <= now:
            slot = None
            _local.pop(key, None)
        if slot is not None and slot[0] >= limit:
            return max(slot[1] - now, 0.001), key
    for key, _limit, window in buckets:
        slot = _local.setdefault(key, [0, now + window])
        slot[0] += 1
    return 0.0, ""


async def _acquire_redis(client: Any, buckets: list[tuple[str, int, int]]) -> tuple[float, str]:
    args: list[Any] = []
    for _key, limit, window in buckets:
        args += [limit, window * 1000]
    out = await client.eval(_ACQUIRE_LUA, len(buckets), *[b[0] for b in buckets], *args)
    wait_ms, index = (list(out) + [0, 0])[:2] if isinstance(out, list | tuple) else (out, 0)
    index = int(index or 0)
    key = buckets[index - 1][0] if 1 <= index <= len(buckets) else ""
    return int(wait_ms or 0) / 1000.0, key


def _scope(key: str) -> str:
    if key.startswith("cf:data:ws:"):
        return "workspace"
    return "r2" if key == "cf:data:r2" else "global"


class RestoreBusy(RateLimited):
    """This database was restored too often just now (429)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "sites.data_restore_busy",
            f"This site's data was restored several times just now. Try again in {retry_after} s.",
        )
        self.details = {"retry_after": retry_after}


async def _wait(buckets: list[tuple[str, int, int]]) -> tuple[float, str]:
    """Charge every bucket and answer ``(0, "")``, or answer the seconds until the
    full one resets and its key (charging nothing)."""
    wait: tuple[float, str] = (0.0, "")
    client = redis_client()
    if client is not None:
        try:
            wait = await _acquire_redis(client, buckets)
        except Exception:  # noqa: BLE001 — a Redis outage must not fail every read
            logger.warning("site_data.budget: Redis unavailable, using in-process buckets")
            wait = _acquire_local(buckets)
    else:
        wait = _acquire_local(buckets)
    return wait


async def acquire(kind: str, workspace_id: str) -> None:
    """Spend one Cloudflare call of ``kind`` (``d1`` / ``kv`` / ``r2``) for
    ``workspace_id``, or raise ``DataBusy`` when any bucket it needs is spent."""
    wait, key = await _wait(_buckets(kind, workspace_id))
    if wait > 0:
        raise DataBusy(max(1, math.ceil(wait)), _scope(key))


async def acquire_restore(database_id: str) -> None:
    """Spend one of the database's restores (3 per 10 minutes), or raise
    ``RestoreBusy``."""
    wait, _key = await _wait([(f"cf:data:restore:{database_id or '-'}", *RESTORE_LIMIT)])
    if wait > 0:
        raise RestoreBusy(max(1, math.ceil(wait)))


class Budgeted:
    """A D1 client whose every API call spends one token for ``workspace_id`` first."""

    def __init__(self, cf: Any, workspace_id: str) -> None:
        self._cf = cf
        self._workspace_id = workspace_id

    async def query_d1(self, *, database_id: str, sql: str, params: list | None = None) -> Any:
        await acquire("d1", self._workspace_id)
        return await self._cf.query_d1(database_id=database_id, sql=sql, params=params)

    async def query_d1_batch(self, *, database_id: str, statements: Any) -> Any:
        await acquire("d1", self._workspace_id)
        return await self._cf.query_d1_batch(database_id=database_id, statements=statements)

    async def d1_info(self, database_id: str) -> Any:
        await acquire("d1", self._workspace_id)
        return await self._cf.d1_info(database_id)

    async def d1_bookmark(self, database_id: str, timestamp: str | None = None) -> str:
        await acquire("d1", self._workspace_id)
        return await self._cf.d1_bookmark(database_id, timestamp)

    async def d1_restore(
        self, database_id: str, *, bookmark: str | None = None, timestamp: str | None = None
    ) -> Any:
        await acquire("d1", self._workspace_id)
        return await self._cf.d1_restore(database_id, bookmark=bookmark, timestamp=timestamp)


# --------------------------------------------------------------------------- one-shot claims

# key -> expires at (monotonic seconds)
_claims: dict[str, float] = {}


async def claim_once(key: str, ttl_s: int) -> bool:
    """Atomically claim ``key`` for ``ttl_s`` seconds: True for the one caller that
    got it, False while anyone else holds it."""
    client = redis_client()
    if client is not None:
        try:
            return bool(await client.set(key, "1", nx=True, ex=max(1, ttl_s)))
        except Exception:  # noqa: BLE001 — fall back to this process
            logger.warning("site_data.budget: Redis unavailable, claiming in process")
    now = time.monotonic()
    held = _claims.get(key)
    if held is not None and held > now:
        return False
    _claims[key] = now + ttl_s
    return True


async def release(key: str) -> None:
    """Give a claim back (the work it guarded is over, or failed before it changed
    anything)."""
    _claims.pop(key, None)
    client = redis_client()
    if client is not None:
        try:
            await client.delete(key)
        except Exception:  # noqa: BLE001 — it expires on its own
            logger.warning("site_data.budget: could not release %s", key)
