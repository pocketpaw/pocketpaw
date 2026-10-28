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
# whole batch counts once) is charged before it is made.
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

KINDS = frozenset({"d1", "kv", "r2"})

# KEYS: the bucket keys. ARGV: limit, window_ms per key, in KEYS order. Returns 0 when
# granted (every key charged), else the milliseconds until the full bucket resets.
_ACQUIRE_LUA = """
for i = 1, #KEYS do
  local used = tonumber(redis.call('GET', KEYS[i]) or '0')
  if used >= tonumber(ARGV[2 * i - 1]) then
    local ttl = redis.call('PTTL', KEYS[i])
    if ttl < 1 then ttl = tonumber(ARGV[2 * i]) end
    return ttl
  end
end
for i = 1, #KEYS do
  local n = redis.call('INCR', KEYS[i])
  if n == 1 or redis.call('PTTL', KEYS[i]) < 0 then
    redis.call('PEXPIRE', KEYS[i], ARGV[2 * i])
  end
end
return 0
"""

_redis: Any | None = None
# key -> [used, window resets at (monotonic seconds)]
_local: dict[str, list[float]] = {}


class DataBusy(RateLimited):
    """The dashboard's Cloudflare budget is spent for now (429)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "sites.data_busy",
            f"Your site's data is busy. Try again in {retry_after} s.",
        )
        self.details = {"retry_after": retry_after}


def use_redis(client: Any | None) -> None:
    """Share the buckets through ``client`` (None goes back to the default choice)."""
    global _redis
    _redis = client


def reset() -> None:
    """Forget every in-process bucket and any injected Redis (tests)."""
    global _redis
    _redis = None
    _local.clear()


def _buckets(kind: str, workspace_id: str) -> list[tuple[str, int, int]]:
    if kind not in KINDS:
        raise ValueError(f"unknown Cloudflare call kind: {kind!r}")
    out = [("cf:data:global", *GLOBAL_LIMIT)]
    if kind == "r2":
        out.append(("cf:data:r2", *R2_LIMIT))
    out.append((f"cf:data:ws:{workspace_id or '-'}", *WORKSPACE_LIMIT))
    return out


def _client() -> Any | None:
    if _redis is not None:
        return _redis
    from pocketpaw_ee.cloud._core.redis_client import get_redis

    try:
        return get_redis()
    except RuntimeError:  # POCKETPAW_REDIS_URL unset: in-process buckets
        return None


def _acquire_local(buckets: list[tuple[str, int, int]]) -> float:
    """0 when granted, else seconds until the full bucket resets."""
    now = time.monotonic()
    for key, limit, window in buckets:
        slot = _local.get(key)
        if slot is not None and slot[1] <= now:
            slot = None
            _local.pop(key, None)
        if slot is not None and slot[0] >= limit:
            return max(slot[1] - now, 0.001)
    for key, _limit, window in buckets:
        slot = _local.setdefault(key, [0, now + window])
        slot[0] += 1
    return 0.0


async def _acquire_redis(client: Any, buckets: list[tuple[str, int, int]]) -> float:
    args: list[Any] = []
    for _key, limit, window in buckets:
        args += [limit, window * 1000]
    wait_ms = await client.eval(_ACQUIRE_LUA, len(buckets), *[b[0] for b in buckets], *args)
    return int(wait_ms or 0) / 1000.0


async def acquire(kind: str, workspace_id: str) -> None:
    """Spend one Cloudflare call of ``kind`` (``d1`` / ``kv`` / ``r2``) for
    ``workspace_id``, or raise ``DataBusy`` when any bucket it needs is spent."""
    buckets = _buckets(kind, workspace_id)
    wait = 0.0
    client = _client()
    if client is not None:
        try:
            wait = await _acquire_redis(client, buckets)
        except Exception:  # noqa: BLE001 — a Redis outage must not fail every read
            logger.warning("site_data.budget: Redis unavailable, using in-process buckets")
            wait = _acquire_local(buckets)
    else:
        wait = _acquire_local(buckets)
    if wait > 0:
        raise DataBusy(max(1, math.ceil(wait)))


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
