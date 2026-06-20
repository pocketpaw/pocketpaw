"""arq worker entry point for Tier 2 run execution.

Updated: 2026-06-20 (feat/workspace-jobs, pp#1459) — registered the workspace
jobs entrypoint ``execute_workspace_job`` into ``WorkerSettings.functions``
(default #1: SAME worker process, no new deploy artifact). It is wrapped with
``arq.worker.func(timeout=...)`` so workspace jobs get their OWN per-function
timeout (``POCKETPAW_JOB_TIMEOUT_SECONDS``, default 900s) without changing the
chat-run timeout. The jobs share this worker's Redis pool + realtime bootstrap.

Deploy as a separate process alongside the web service::

    arq pocketpaw_ee.cloud.chat.runs.worker.WorkerSettings

The worker owns the agent run; the web process just enqueues
``execute_run_job`` via ``ArqExecutor`` and streams events back through Redis.

On boot, if ``POCKETPAW_CLOUD_WORKER_BOOT_SWEEP=true`` (single-replica only —
multi-replica would interrupt sibling workers' in-flight runs), sweep any
``queued``/``running`` leftovers as ``interrupted``. LLM streams can't resume
mid-generation; the partial already streamed remains visible, the user
retries manually. HA deploys rely on the 10-minute heartbeat sweeper instead.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from arq.connections import RedisSettings
from arq.worker import func

# Imported at module scope so tests can ``monkeypatch.setattr(worker, …)``.
from pocketpaw_ee.cloud import init_realtime
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.run_core import execute_run
from pocketpaw_ee.cloud.chat.runs.sweeper import sweep_stale_runs
from pocketpaw_ee.cloud.jobs.domain import job_timeout_seconds
from pocketpaw_ee.cloud.jobs.worker import execute_workspace_job
from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db

logger = logging.getLogger(__name__)


# A short cutoff because worker boot implies the previous worker just died;
# runs created seconds ago by the web process should not be swept.
_BOOT_SWEEP_OLDER_THAN_SECONDS = 5

# Default off — multi-replica safety. See module docstring.
_BOOT_SWEEP_ENV = "POCKETPAW_CLOUD_WORKER_BOOT_SWEEP"


def _boot_sweep_enabled() -> bool:
    return os.environ.get(_BOOT_SWEEP_ENV, "").strip().lower() == "true"


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
    if not _boot_sweep_enabled():
        logger.info("worker boot: stale-run sweep disabled (%s)", _BOOT_SWEEP_ENV)
        return
    try:
        swept = await sweep_stale_runs(older_than_seconds=_BOOT_SWEEP_OLDER_THAN_SECONDS)
        if swept:
            logger.info("worker boot: marked %d orphaned runs as interrupted", swept)
    except Exception:
        logger.exception("worker boot: stale-run sweep failed")


async def _shutdown(ctx: dict[str, Any]) -> None:
    await close_cloud_db()


def _redis_settings() -> RedisSettings:
    """Resolve the arq RedisSettings from ``POCKETPAW_REDIS_URL``.

    Why eager (called at module import / class-body evaluation):

    arq's ``worker.get_kwargs`` reads ``settings_cls.__dict__`` directly to
    build the Worker (arq 0.28, ``worker.py:889``). ``__dict__`` access
    bypasses the descriptor protocol, so a non-data descriptor here would
    end up handed to ``Worker.__init__`` as-is — arq would crash when it
    tried to use it as a RedisSettings. Eager evaluation is the only shape
    that survives arq's attribute-access pattern AND fails loud when the
    env var is missing (review finding #4 — silent fallback to localhost
    split-brained typoed prod deploys).

    Tests set ``POCKETPAW_REDIS_URL`` in ``tests/cloud/conftest.py`` before
    any test module is imported so this import-time read succeeds.
    """
    url = os.environ.get("POCKETPAW_REDIS_URL", "").strip()
    if not url:
        raise RuntimeError("POCKETPAW_REDIS_URL must be set to run the Tier 2 arq worker")
    return RedisSettings.from_dsn(url)


# Workspace jobs (pp#1459) run on this same worker but get their OWN
# per-function timeout via ``arq.worker.func`` so a long-running job can't be
# clipped by the chat-run timeout and vice-versa. The dotted name the web
# process enqueues (``"execute_workspace_job"``) is the function's __qualname__
# by default; pin it explicitly so the enqueue/registration names can't drift.
_workspace_job_fn = func(
    execute_workspace_job,
    name="execute_workspace_job",
    timeout=job_timeout_seconds(),
    max_tries=1,
)


class WorkerSettings:
    """arq worker configuration. Loaded by ``arq <dotted-path>``."""

    functions = [execute_run_job, _workspace_job_fn]
    on_startup = _startup
    on_shutdown = _shutdown
    # Crash policy: no auto-retry. A failed run is left as ``failed``/``interrupted``
    # so the user can decide whether to resend — re-running could double-bill or
    # surface a partial duplicate.
    max_tries = 1
    # Eager: arq reads __dict__, which bypasses descriptors. See `_redis_settings`.
    redis_settings = _redis_settings()
