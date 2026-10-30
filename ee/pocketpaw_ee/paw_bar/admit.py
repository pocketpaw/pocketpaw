# Paw Bar event admission: the per-widget rate check plus the event write.
#
# With POCKETPAW_REDIS_URL set, a Lua script decides admission atomically in
# Redis: a sliding 60 s window per (widget, bucket), overall and per
# customer_ref, the same rule PawBarStore.admit_event applies. Only admitted
# events are then written to SQLite as a plain insert. That keeps SQLite's
# exclusive write lock (BEGIN IMMEDIATE) off the hot path, which is what
# serialized every process on one file.
#
# Without Redis, or when Redis errors, it falls back to store.admit_event, so
# self-hosted single-process installs behave as before and a Redis outage costs
# speed, not correctness. After a Redis error this process skips Redis for
# _BACKOFF_S, so an outage does not add a connect timeout to every event.
#
# Invariant: every admitted event still becomes a SQLite row. within_rate_limit
# and count_events_since (read gates, gated-action cap, handoff) count those
# rows. If the insert fails after Redis admitted, Redis has counted an event
# that was never stored, which errs strict. The caller sees the insert's error.

from __future__ import annotations

import logging
import os
import time
import uuid
from typing import Any

from pocketpaw.paw_bar.models import PawBarEvent, PawBarWidget

logger = logging.getLogger(__name__)

_WINDOW_MS = 60_000
_KEY_PREFIX = "pawbar:rl:"
_BACKOFF_S = 30.0
_redis_skip_until = 0.0

# KEYS[1] overall zset, KEYS[2] per-customer zset.
# ARGV: now_ms, window_ms, overall cap, per-customer cap, member.
# Returns 1 when admitted (and recorded in both sets), 0 when refused.
_ADMIT_LUA = """
local cutoff = tonumber(ARGV[1]) - tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', cutoff)
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
if redis.call('ZCARD', KEYS[2]) >= tonumber(ARGV[4]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[5])
redis.call('ZADD', KEYS[2], ARGV[1], ARGV[5])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
redis.call('PEXPIRE', KEYS[2], ARGV[2])
return 1
"""


def _keys(widget_id: str, bucket: str, customer_ref: str) -> list[str]:
    base = f"{_KEY_PREFIX}{widget_id}:{bucket}"
    return [base, f"{base}:c:{customer_ref}"]


async def redis_admit(
    redis: Any,
    event: PawBarEvent,
    *,
    overall_per_min: int,
    per_customer_per_min: int,
    bucket: str = "",
    now_ms: int | None = None,
) -> bool:
    """Count ``event`` against the widget's window in Redis. Writes nothing to SQLite."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    ok = await redis.eval(
        _ADMIT_LUA,
        2,
        *_keys(event.widget_id, bucket, event.customer_ref),
        now_ms,
        _WINDOW_MS,
        overall_per_min,
        per_customer_per_min,
        uuid.uuid4().hex,
    )
    return bool(int(ok))


def _redis_or_none() -> Any | None:
    if not os.environ.get("POCKETPAW_REDIS_URL", "").strip():
        return None
    if time.monotonic() < _redis_skip_until:
        return None
    from pocketpaw_ee.cloud._core.redis_client import get_redis

    return get_redis()


async def admit(store: Any, event: PawBarEvent, widget: PawBarWidget, *, bucket: str = "") -> bool:
    """Admit and record ``event``. True when recorded, False when rate limited.

    Raises whatever ``store.admit_event`` or ``store.record_event`` raises
    (``sqlite3.OperationalError`` on a locked store); callers keep their own
    fail-open or fail-closed handling."""
    limits = {
        "overall_per_min": widget.rate_limit_per_min,
        "per_customer_per_min": widget.per_customer_limit_per_min,
        "bucket": bucket,
    }
    redis = _redis_or_none()
    if redis is not None:
        try:
            admitted = await redis_admit(redis, event, **limits)
        except Exception:
            global _redis_skip_until
            _redis_skip_until = time.monotonic() + _BACKOFF_S
            logger.warning(
                "paw-bar redis admit failed; using the store for %ss", _BACKOFF_S, exc_info=True
            )
        else:
            if admitted:
                await store.record_event(event, bucket=bucket)
            return admitted
    return await store.admit_event(event, **limits)
