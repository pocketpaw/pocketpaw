"""Tier 2 executor — enqueues the run as an arq job for a separate worker
process to execute. Selected when ``POCKETPAW_CLOUD_RUN_EXECUTOR=arq``.

The arq pool is lazily constructed on first ``submit`` and cached for the
lifetime of the process.
"""

from __future__ import annotations

import asyncio
import logging
import os

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

logger = logging.getLogger(__name__)

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
        pool = await _get_pool()
        # RunSpec is intentionally JSON-primitive so it survives the
        # arq/pickle boundary without custom serialisers.
        await pool.enqueue_job("execute_run_job", spec.model_dump())


def _reset_for_tests() -> None:
    global _pool
    _pool = None
