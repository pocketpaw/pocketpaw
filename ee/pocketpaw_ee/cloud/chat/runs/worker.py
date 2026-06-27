"""arq worker entry point for Tier 2 run execution.

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


# arq's DEFAULT job_timeout is 300s (5 min), which CANCELS a long agent run
# mid-generation: a big coding task in /chat halts after ~5 min with only the
# partial that already streamed persisted (run_core catches the CancelledError and
# emits a cancelled stream_end). Lift the cap to a generous default and make it
# env-tunable; the 10-minute stale-run sweeper remains the backstop against a
# genuinely runaway run holding a worker slot.
_DEFAULT_RUN_JOB_TIMEOUT_SECONDS = 1800  # 30 minutes


def _job_timeout_seconds() -> int:
    """Resolve the per-run arq job_timeout from ``POCKETPAW_CLOUD_RUN_JOB_TIMEOUT``.

    Defaults to 30 minutes. An unparseable or non-positive value falls back to the
    default (rather than 0 / negative, which would disable or break the cap), so a
    typo can't silently let runs run forever or crash the worker.
    """
    raw = os.environ.get("POCKETPAW_CLOUD_RUN_JOB_TIMEOUT", "").strip()
    if not raw:
        return _DEFAULT_RUN_JOB_TIMEOUT_SECONDS
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "POCKETPAW_CLOUD_RUN_JOB_TIMEOUT=%r is not an int; using default %ds",
            raw,
            _DEFAULT_RUN_JOB_TIMEOUT_SECONDS,
        )
        return _DEFAULT_RUN_JOB_TIMEOUT_SECONDS
    if val <= 0:
        logger.warning(
            "POCKETPAW_CLOUD_RUN_JOB_TIMEOUT=%d is not positive; using default %ds",
            val,
            _DEFAULT_RUN_JOB_TIMEOUT_SECONDS,
        )
        return _DEFAULT_RUN_JOB_TIMEOUT_SECONDS
    return val


class WorkerSettings:
    """arq worker configuration. Loaded by ``arq <dotted-path>``."""

    functions = [execute_run_job]
    on_startup = _startup
    on_shutdown = _shutdown
    # Crash policy: no auto-retry. A failed run is left as ``failed``/``interrupted``
    # so the user can decide whether to resend — re-running could double-bill or
    # surface a partial duplicate.
    max_tries = 1
    # Per-run timeout. arq's default (300s) cancels long agent runs mid-stream; lift
    # it and make it env-tunable (POCKETPAW_CLOUD_RUN_JOB_TIMEOUT, default 30 min).
    # Plain int in __dict__ for the same arq-reads-__dict__ reason as redis_settings.
    job_timeout = _job_timeout_seconds()
    # Eager: arq reads __dict__, which bypasses descriptors. See `_redis_settings`.
    redis_settings = _redis_settings()
