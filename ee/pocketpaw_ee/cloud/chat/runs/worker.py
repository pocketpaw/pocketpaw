"""arq worker entry point for Tier 2 run execution.

Updated: 2026-06-22 (feat/jobs-custom-job-entrypoints) — ``_startup`` now also
calls ``load_entrypoint_jobs()`` right after ``register_builtins()`` so the
worker registers WORKSPACE-CUSTOM jobs (declared under the ``pocketpaw.jobs``
entry-point group) in its own process. Without this a custom job would resolve
in the web process but raise ``UnknownJobError`` in the worker that runs it.
No-op when no custom-job package is installed.

Updated: 2026-06-22 (feat/jobs-worker-register-and-connector-read) — PRODUCTION
FIX: ``_startup`` now calls ``register_builtins()`` AFTER ``init_realtime()`` so
the worker process populates the process-wide job registry on boot. The registry
is a module-level dict; the worker runs in a SEPARATE process from the web
``mount_cloud`` that registered the built-ins there, so without this the
worker's registry was EMPTY and ``execute_workspace_job`` → ``resolve_job(name)``
raised ``UnknownJobError`` for EVERY job in a real deploy. Mirrors the ordering
in ``ee/pocketpaw_ee/cloud/__init__.py:mount_cloud`` (register after
init_realtime so a job's writeback emit has a bus to publish onto).

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

Updated: 2026-06-24 (integration/billing-credits, BC-3) — the boot sweep now
also runs the compute-cost metering sweep (``sweep_unbilled_runs``) so any
terminal runs the prior worker left unbilled are charged on restart. The
metering sweep is idempotent (billed flag + ``run:{run_id}`` ledger key), so it
is safe even when the boot stale-run sweep is disabled — it just bills the
already-terminal backlog.

Updated: 2026-06-26 (feat/litellm-billing-cutover, WU-F) — the boot sweep also
runs the per-tenant LiteLLM billing-cutover sweep (``run_cutover_sweep``): a
no-op in ``off`` mode, a read-only reconciliation compare in ``shadow``, and a
proxy-spend debit in ``live`` (where ``sweep_unbilled_runs`` self-gates off so
exactly one meter charges). Idempotent + its own try so a cutover-sweep failure
can't abort worker startup.
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
from pocketpaw_ee.cloud.metering.sweeper import sweep_unbilled_runs
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
    # Register the built-in workspace jobs into the process-wide registry. The
    # registry is a module-level dict and the worker runs in its OWN process, so
    # the registration ``mount_cloud`` does for the web process does NOT carry
    # over here. Without this the worker's registry is empty and
    # ``execute_workspace_job`` → ``resolve_job(name)`` raises ``UnknownJobError``
    # for every job. Called AFTER ``init_realtime()`` (same ordering as
    # ``mount_cloud``) so a job's writeback emit has a bus to publish onto.
    from pocketpaw_ee.cloud.jobs.builtin import register_builtins

    register_builtins()
    # Discover + register WORKSPACE-CUSTOM jobs from installed packages' entry
    # points (group ``pocketpaw.jobs``). The worker runs in its OWN process, so
    # like the built-ins these must be registered here too — otherwise a custom
    # job dispatched by the web app would ``resolve_job`` fine there but raise
    # ``UnknownJobError`` in the worker that actually runs it. Called AFTER
    # ``register_builtins()`` (same ordering as ``mount_cloud``). No-op when no
    # custom-job package is installed.
    from pocketpaw_ee.cloud.jobs.plugins import load_entrypoint_jobs

    load_entrypoint_jobs()
    if not _boot_sweep_enabled():
        logger.info("worker boot: stale-run sweep disabled (%s)", _BOOT_SWEEP_ENV)
        return
    try:
        swept = await sweep_stale_runs(older_than_seconds=_BOOT_SWEEP_OLDER_THAN_SECONDS)
        if swept:
            logger.info("worker boot: marked %d orphaned runs as interrupted", swept)
    except Exception:
        logger.exception("worker boot: stale-run sweep failed")
    # BC-3 metering: bill any terminal runs the prior worker left unbilled (the
    # boot sweep above just turned this boot's orphans terminal, and earlier
    # finished runs may never have been billed). Own try so a metering failure
    # can't abort worker startup. Self-gated OFF in the WU-F ``live`` cutover mode.
    try:
        billed = await sweep_unbilled_runs()
        if billed:
            logger.info("worker boot: billed %d unbilled terminal runs", billed)
    except Exception:
        logger.exception("worker boot: compute-cost metering sweep failed")
    # WU-F billing cutover: per-tenant LiteLLM spend sweep on boot too (no-op in
    # ``off``; shadow-compare in ``shadow``; debit proxy spend in ``live``). Own try
    # so a cutover-sweep failure can't abort worker startup.
    try:
        from pocketpaw_ee.cloud.llm_provisioning.cutover_sweeper import run_cutover_sweep

        summary = await run_cutover_sweep()
        if summary.get("processed"):
            logger.info("worker boot: cutover sweep processed %d tenants", summary["processed"])
    except Exception:
        logger.exception("worker boot: LiteLLM billing-cutover sweep failed")


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


# arq's DEFAULT job_timeout is 300s (5 min), which CANCELS a long CHAT-RUN agent
# run mid-generation: a big coding task in /chat halts after ~5 min with only the
# partial that already streamed persisted (run_core catches the CancelledError and
# emits a cancelled stream_end). Lift the cap to a generous default and make it
# env-tunable; the 10-minute stale-run sweeper remains the backstop against a
# genuinely runaway run holding a worker slot. (Workspace jobs get their OWN
# per-function timeout below, so the two can't clip each other.)
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
    # Per-run timeout. arq's default (300s) cancels long agent runs mid-stream; lift
    # it and make it env-tunable (POCKETPAW_CLOUD_RUN_JOB_TIMEOUT, default 30 min).
    # Plain int in __dict__ for the same arq-reads-__dict__ reason as redis_settings.
    job_timeout = _job_timeout_seconds()
    # Eager: arq reads __dict__, which bypasses descriptors. See `_redis_settings`.
    redis_settings = _redis_settings()
