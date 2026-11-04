# ee/pocketpaw_ee/sites/site_data/dashboard_cache.py — the shared result cache for Sites
# AI dashboard widgets, so many viewers of one dashboard cost one D1 read.
#
# A widget query's raw D1 result is cached under ``dash:res:<d1>:<sha(sql, params)>:
# <schema fingerprint>:<epoch>`` for ``min(widget interval, 60 s)``. The schema
# fingerprint changes on any migration, and ``epoch`` is a per-database counter the Data
# page bumps on every write it makes (rows, imports, console writes, restores), so a
# cached result never outlives a change the owner made through Paw. Errors are never
# cached. Cells are clipped before they are stored, so one huge text column can't
# fill Redis.
#
# Storage is the dashboard budget's Redis client (``budget.redis_client``) when Redis is
# configured, else this process; a Redis error falls back to this process rather than
# failing the read. Values are JSON with ``cached_at`` (ISO, UTC) beside the result.
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from pocketpaw_ee.sites.site_data import budget
from pocketpaw_ee.sites.site_data.dashboard_sql import CELL_MAX, GatedQuery

logger = logging.getLogger(__name__)

MAX_TTL_S = 60
EPOCH_TTL_S = 7 * 24 * 3600
_LOCAL_MAX = 512

# key -> (expires at monotonic, value)
_local: dict[str, tuple[float, Any]] = {}


def reset() -> None:
    """Forget every in-process entry (tests)."""
    _local.clear()


def _local_get(key: str) -> Any | None:
    hit = _local.get(key)
    if hit is None or hit[0] <= time.monotonic():
        _local.pop(key, None)
        return None
    return hit[1]


def _local_put(key: str, value: Any, ttl: float) -> None:
    if len(_local) >= _LOCAL_MAX:
        _local.pop(next(iter(_local)))
    _local[key] = (time.monotonic() + ttl, value)


def _epoch_key(database_id: str) -> str:
    return f"dash:epoch:{database_id or '-'}"


async def epoch(database_id: str) -> int:
    """The database's current write epoch (0 before its first Data-page write)."""
    client = budget.redis_client()
    if client is not None:
        try:
            return int(await client.get(_epoch_key(database_id)) or 0)
        except Exception:  # noqa: BLE001 — fall back to this process
            logger.warning("site_dashboards: Redis unavailable, reading the epoch in process")
    return int(_local_get(_epoch_key(database_id)) or 0)


async def bump(database_id: str) -> None:
    """Invalidate every cached result of the database (the owner just changed it)."""
    if not database_id:
        return
    key = _epoch_key(database_id)
    _local_put(key, int(_local_get(key) or 0) + 1, EPOCH_TTL_S)
    client = budget.redis_client()
    if client is not None:
        try:
            await client.incr(key)
            await client.expire(key, EPOCH_TTL_S)
        except Exception:  # noqa: BLE001 — a write must never fail on the cache
            logger.warning("site_dashboards: could not bump the epoch of %s", database_id)


def key(database_id: str, q: GatedQuery, schema_fp: str, epoch_n: int) -> str:
    body = json.dumps([q.sql, q.params], default=str).encode()
    digest = hashlib.sha256(body).hexdigest()[:32]
    return f"dash:res:{database_id or '-'}:{digest}:{schema_fp[:16]}:{epoch_n}"


def _clip(raw: dict[str, Any]) -> dict[str, Any]:
    rows = [
        [v[:CELL_MAX] + "…" if isinstance(v, str) and len(v) > CELL_MAX else v for v in r]
        for r in raw.get("rows") or []
    ]
    return {"columns": list(raw.get("columns") or []), "rows": rows}


async def get(cache_key: str) -> tuple[dict[str, Any], str] | None:
    """``(raw result, cached_at)`` or None."""
    text: Any = None
    client = budget.redis_client()
    if client is not None:
        try:
            text = await client.get(cache_key)
        except Exception:  # noqa: BLE001
            logger.warning("site_dashboards: Redis unavailable, reading the cache in process")
            text = _local_get(cache_key)
    else:
        text = _local_get(cache_key)
    if not text:
        return None
    try:
        value = json.loads(text)
        return value["raw"], str(value["at"])
    except (ValueError, KeyError, TypeError):
        return None


async def put(cache_key: str, raw: dict[str, Any], at: str, ttl_s: int) -> None:
    ttl = max(1, min(int(ttl_s), MAX_TTL_S))
    text = json.dumps({"raw": _clip(raw), "at": at}, default=str)
    client = budget.redis_client()
    if client is not None:
        try:
            await client.set(cache_key, text, ex=ttl)
            return
        except Exception:  # noqa: BLE001
            logger.warning("site_dashboards: Redis unavailable, caching in process")
    _local_put(cache_key, text, ttl)
