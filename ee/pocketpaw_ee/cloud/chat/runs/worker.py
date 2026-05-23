"""arq worker entry point for Tier 2 run execution.

Deploy as a separate process alongside the web service::

    arq pocketpaw_ee.cloud.chat.runs.worker.WorkerSettings

The worker owns the agent run; the web process just enqueues
``execute_run_job`` via ``ArqExecutor`` and streams events back through Redis.

On boot, the worker sweeps any run left in ``queued``/``running`` by the
previous worker (it crashed, otherwise we wouldn't be starting) and marks
them ``interrupted``. LLM token streams cannot resume mid-generation, so the
sweep does not re-enqueue — the partial already streamed remains visible and
the user retries manually.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from arq.connections import RedisSettings

# Imported at module scope so tests can ``monkeypatch.setattr(worker, …)``.
from pocketpaw_ee.cloud import init_realtime
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.run_core import execute_run
from pocketpaw_ee.cloud.chat.runs.sweeper import sweep_stale_runs
from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db

logger = logging.getLogger(__name__)


# A short cutoff because worker boot implies the previous worker just died;
# runs created seconds ago by the web process should not be swept.
_BOOT_SWEEP_OLDER_THAN_SECONDS = 5


async def execute_run_job(ctx: dict[str, Any], spec_dict: dict[str, Any]) -> None:
    """arq job entrypoint — rehydrate the RunSpec and run the agent."""
    spec = RunSpec.model_validate(spec_dict)
    logger.info("worker: starting run %s", spec.run_id)
    await execute_run(spec)


async def _startup(ctx: dict[str, Any]) -> None:
    """Boot the worker: pin role, init the DB + realtime bus, sweep orphans.

    ``xproc.set_role("worker")`` must run before any agent code emits, so
    ``emit()`` and the run-side broadcast helpers route over the bridge
    instead of into the worker's empty local bus / WS manager.
    """
    xproc.set_role("worker")
    mongo_uri = os.environ.get("CLOUD_MONGODB_URI", "mongodb://localhost:27017/paw-enterprise")
    await init_cloud_db(mongo_uri)
    init_realtime()
    try:
        swept = await sweep_stale_runs(older_than_seconds=_BOOT_SWEEP_OLDER_THAN_SECONDS)
        if swept:
            logger.info("worker boot: marked %d orphaned runs as interrupted", swept)
    except Exception:
        logger.exception("worker boot: stale-run sweep failed")


async def _shutdown(ctx: dict[str, Any]) -> None:
    await close_cloud_db()


class _LazyRedisSettings:
    """Descriptor that reads ``POCKETPAW_REDIS_URL`` on access, not at module
    import. arq accesses ``WorkerSettings.redis_settings`` once at worker
    boot, so this is effectively boot-time evaluation.

    Two reasons it isn't a plain class attribute:
    - Import-time read froze the env at first import (review finding #7),
      which broke test ergonomics and any deploy that loads env after the
      module is imported.
    - Defaulting silently to ``redis://localhost:6379/0`` when the env is
      unset (review finding #4) split-brain a typoed prod deploy — web
      enqueued to prod-Redis, worker waited on localhost.
    """

    def __get__(self, obj: object, objtype: type | None = None) -> RedisSettings:
        url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
        if not url:
            raise RuntimeError("POCKETPAW_REDIS_URL must be set to run the Tier 2 arq worker")
        return RedisSettings.from_dsn(url)


class WorkerSettings:
    """arq worker configuration. Loaded by ``arq <dotted-path>``."""

    functions = [execute_run_job]
    on_startup = _startup
    on_shutdown = _shutdown
    # Crash policy: no auto-retry. A failed run is left as ``failed``/``interrupted``
    # so the user can decide whether to resend — re-running could double-bill or
    # surface a partial duplicate.
    max_tries = 1
    redis_settings = _LazyRedisSettings()
