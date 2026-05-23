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
    """Boot the worker: init the DB + realtime bus, then sweep orphans."""
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


class WorkerSettings:
    """arq worker configuration. Loaded by ``arq <dotted-path>``."""

    functions = [execute_run_job]
    on_startup = _startup
    on_shutdown = _shutdown
    # Crash policy: no auto-retry. A failed run is left as ``failed``/``interrupted``
    # so the user can decide whether to resend — re-running could double-bill or
    # surface a partial duplicate.
    max_tries = 1
    redis_settings = RedisSettings.from_dsn(
        os.environ.get("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
    )
