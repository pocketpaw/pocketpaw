"""Tier 2 executor — enqueues the run as an arq job for a separate worker
process to execute. Selected when ``POCKETPAW_CLOUD_RUN_EXECUTOR=arq``.

The arq pool is the process-wide one from ``_core.redis_client.get_arq_pool``
(built on first ``submit``, closed by ``CloudLifecycleHook.on_shutdown``).
Changes (2026-10-01, CN-4): the local getter and ``close_pool`` moved there.

Before enqueueing, ``submit`` writes a ``queued`` frame to the run's stream (only
when the stream does not exist yet, so an idempotent re-submit never puts it
after the worker's frames). Without it a run waiting for a free worker slot has
no stream at all and its reader sees nothing but keep-alive pings. The frame's
TTL (``queued_stream_ttl_seconds``) bounds the key until a terminal write
refreshes it. Best-effort: a failed frame never blocks the enqueue.
"""

from __future__ import annotations

import logging

from pocketpaw_ee.cloud._core.redis_client import get_arq_pool
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec, queued_stream_ttl_seconds

logger = logging.getLogger(__name__)

QUEUED_MESSAGE = "Waiting for a free slot. Your reply will start shortly."

# The process-wide arq pool (one pool, closed on shutdown) lives in
# _core.redis_client. ``_get_pool`` is this module's name for it: callers and
# tests monkeypatch it here.
_get_pool = get_arq_pool


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
