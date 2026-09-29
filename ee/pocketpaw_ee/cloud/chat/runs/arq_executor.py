"""Tier 2 executor — enqueues the run as an arq job for a separate worker
process to execute. Selected when ``POCKETPAW_CLOUD_RUN_EXECUTOR=arq``.

The arq pool is lazily constructed on first ``submit`` and cached for the
lifetime of the process.

Before enqueueing, ``submit`` writes a ``queued`` frame to the run's stream (only
when the stream does not exist yet, so an idempotent re-submit never puts it
after the worker's frames). Without it a run waiting for a free worker slot has
no stream at all and its reader sees nothing but keep-alive pings. The frame's
TTL (``queued_stream_ttl_seconds``) bounds the key until a terminal write
refreshes it. Best-effort: a failed frame never blocks the enqueue.
"""

from __future__ import annotations

import asyncio
import logging
import os

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from pocketpaw_ee.cloud.chat.runs.domain import RunSpec, queued_stream_ttl_seconds

logger = logging.getLogger(__name__)

QUEUED_MESSAGE = "Waiting for a free slot. Your reply will start shortly."

_pool: ArqRedis | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> ArqRedis:
    global _pool
    # Double-checked lock so concurrent first-submits don't leak pools.
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
                if not url:
                    raise RuntimeError(
                        "POCKETPAW_REDIS_URL is not set — the arq executor needs Redis."
                    )
                _pool = await create_pool(RedisSettings.from_dsn(url))
    return _pool


async def close_pool() -> None:
    """Close the cached arq Redis pool on web-process shutdown.

    No-op when the pool was never built (Tier 0 / Tier 1 deployments).
    A failing aclose is swallowed — shutdown paths can't afford to raise.
    """
    global _pool
    pool = _pool
    _pool = None
    if pool is None:
        return
    try:
        await pool.aclose()
    except Exception:
        logger.debug("arq pool aclose failed during shutdown", exc_info=True)


class ArqExecutor:
    """RunExecutor impl — enqueues an ``execute_run_job`` for the worker."""

    async def submit(self, spec: RunSpec) -> None:
        await _announce_queued(spec.run_id)
        pool = await _get_pool()
        # RunSpec is intentionally JSON-primitive so it survives the
        # arq/pickle boundary without custom serialisers.
        await pool.enqueue_job("execute_run_job", spec.model_dump())


async def _announce_queued(run_id: str) -> None:
    """Write the ``queued`` frame (and its TTL) if the run has no stream yet."""
    from pocketpaw_ee.cloud.chat.runs.transport import get_stream_transport

    try:
        transport = get_stream_transport()
        if await transport.stream_exists(run_id):
            return
        await transport.append_event(
            run_id, "queued", {"run_id": run_id, "status": "queued", "message": QUEUED_MESSAGE}
        )
        await transport.set_ttl(run_id, queued_stream_ttl_seconds())
    except Exception:
        logger.warning("queued frame write failed for run %s", run_id, exc_info=True)


def _reset_for_tests() -> None:
    global _pool
    _pool = None
