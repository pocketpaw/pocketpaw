# ee/pocketpaw_ee/cloud/growth/worker.py — arq worker seam for the dedicated
# ``growth`` queue (G-1, feat/growth-g1). Registration only: no real jobs yet.
# Later slices (ingestion, drafts, gated-send dispatch) register their
# entrypoints into ``WorkerSettings.functions`` here and enqueue with
# ``_queue_name=GROWTH_QUEUE_NAME`` (arq's selector is the underscore-prefixed
# kwarg; a bare ``queue=`` is forwarded to the job function and crashes it —
# see the jobs/domain.py history).
#
# Unlike workspace jobs (which ride arq's DEFAULT queue on the shared chat-runs
# worker — see ``jobs/domain.py``), growth gets its OWN queue + worker process
# so a burst of outbound work can never starve interactive chat runs.
#
# Deploy as a separate process alongside the web service::
#
#     arq pocketpaw_ee.cloud.growth.worker.WorkerSettings
#
# The startup/shutdown pair mirrors ``chat/runs/worker.py``: pin the xproc role
# BEFORE anything emits, init the cloud DB + realtime bus, close the DB on the
# way out. ``redis_settings`` is evaluated eagerly at class-body time because
# arq reads ``settings_cls.__dict__`` directly (bypassing descriptors) — the
# same shape + rationale as the chat-runs worker; tests set a stub
# ``POCKETPAW_REDIS_URL`` in ``tests/cloud/conftest.py`` before import.

"""arq worker entry point for the dedicated ``growth`` queue (seam only)."""

from __future__ import annotations

import logging
import os
from typing import Any

from arq.connections import RedisSettings

from pocketpaw_ee.cloud import init_realtime
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud.growth.domain import GROWTH_QUEUE_NAME
from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db

logger = logging.getLogger(__name__)


async def growth_heartbeat(ctx: dict[str, Any]) -> None:
    """Placeholder entrypoint — the queue-registration seam.

    arq refuses to boot a worker with zero functions ("at least one function
    or cron_job must be registered"), so the seam ships this no-op. Later
    slices replace/extend it with the real ingestion / draft / send jobs.
    """
    logger.info("growth worker: heartbeat (no growth jobs registered yet)")


async def _startup(ctx: dict[str, Any]) -> None:
    """Boot the worker: pin role, init the DB + realtime bus.

    ``xproc.set_role("worker")`` must run before any code emits, so ``emit()``
    routes over the cross-process bridge instead of into the worker's empty
    local bus — same ordering as the chat-runs worker.
    """
    xproc.set_role("worker")
    mongo_uri = os.environ.get("CLOUD_MONGODB_URI", "mongodb://localhost:27017/paw-enterprise")
    await init_cloud_db(mongo_uri)
    init_realtime()


async def _shutdown(ctx: dict[str, Any]) -> None:
    await close_cloud_db()


def _redis_settings() -> RedisSettings:
    """Resolve arq RedisSettings from ``POCKETPAW_REDIS_URL``.

    Eager (called at class-body evaluation) because arq's ``get_kwargs`` reads
    ``settings_cls.__dict__`` directly, bypassing the descriptor protocol —
    same rationale as ``chat/runs/worker.py``. Fails loud when the env var is
    missing rather than silently falling back to localhost.
    """
    url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
    if not url:
        raise RuntimeError("POCKETPAW_REDIS_URL must be set to run the growth arq worker")
    return RedisSettings.from_dsn(url)


class WorkerSettings:
    """arq worker configuration for the ``growth`` queue.

    Loaded by ``arq pocketpaw_ee.cloud.growth.worker.WorkerSettings``.
    """

    queue_name = GROWTH_QUEUE_NAME
    functions = [growth_heartbeat]
    on_startup = _startup
    on_shutdown = _shutdown
    # Crash policy mirrors the chat-runs worker: no auto-retry. Outbound work
    # must never double-send on a retry; failed jobs surface for manual review.
    max_tries = 1
    # Eager: arq reads __dict__, which bypasses descriptors. See `_redis_settings`.
    redis_settings = _redis_settings()


__all__ = ["GROWTH_QUEUE_NAME", "WorkerSettings", "growth_heartbeat"]
