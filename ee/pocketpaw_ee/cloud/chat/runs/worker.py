"""arq worker for chat runs: the default-queue lane and the shared worker bootstrap.

The web process enqueues ``execute_run_job`` via ``ArqExecutor``; this lane runs it
and streams events back through Redis. It runs beside the sites lane
(``pocketpaw_ee.sites.build_worker``) in one container under
``pocketpaw_ee.cloud.worker_supervisor``, which drains in-flight jobs on SIGTERM.

Registered functions, each with its own arq timeout so none clips another: chat runs
(``POCKETPAW_CLOUD_RUN_JOB_TIMEOUT``, default 30 min), workspace jobs and the two
/ship jobs (``POCKETPAW_JOB_TIMEOUT_SECONDS``), and the two site builds (their build
budget, which must outlast the in-sandbox ``timeout(1)``). The site builds stay
registered here too so a backlog left on the default queue by an older deploy still
has a consumer. ``max_jobs`` (``POCKETPAW_ARQ_MAX_JOBS``, default 10) is this lane's
ceiling; replicas multiply it. Raise it with the container's memory limit: the
claude_agent_sdk backend spawns a Node process per run.

Retry policy: ``max_tries = 1`` for everything except ``execute_run_job``, which allows
``MAX_RUN_ATTEMPTS`` (2). A run the worker cancelled while shutting down requeues its
run doc (see ``run_core``) and arq re-runs the job on the next worker; that second try
drives the run only if the doc is still ``queued``, with ``retry=True`` so the gates
that spend are not charged twice. Every other failure records itself and is not
retried: re-running could double-bill or repeat a deploy.

Bootstrap (``_startup``, once per process however many lanes share it): pin the xproc
role to ``worker``, init Mongo and the realtime bus, register the built-in and
entry-point workspace jobs (the registry is per process), then, only when
``POCKETPAW_CLOUD_WORKER_BOOT_SWEEP=true`` (single replica), sweep runs the previous
worker orphaned to ``interrupted``, bill unbilled terminal runs, and run the LiteLLM
billing-cutover sweep. Multi-replica deploys rely on the web heartbeat sweeper.
``_shutdown`` (last lane out) waits, bounded, for cancelled runs' shielded cleanups
before closing Mongo, so a requeue or a partial reply is never cut off mid-write.

The health key refreshes every ``POCKETPAW_ARQ_HEALTH_CHECK_INTERVAL`` seconds
(default 30) so ``arq --check`` means "alive now", not "alive this hour".
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from arq.connections import RedisSettings
from arq.worker import func

# Imported at module scope so tests can ``monkeypatch.setattr(worker, …)``.
from pocketpaw_ee.cloud import init_realtime
from pocketpaw_ee.cloud._core.realtime import xproc
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.chat.runs.domain import (
    DEFAULT_RUN_JOB_TIMEOUT_SECONDS,
    MAX_RUN_ATTEMPTS,
    RunSpec,
    run_job_timeout_seconds,
)
from pocketpaw_ee.cloud.chat.runs.run_core import (
    _heartbeat_seconds,
    drain_pending_cleanups,
    execute_run,
)
from pocketpaw_ee.cloud.chat.runs.sweeper import sweep_stale_runs
from pocketpaw_ee.cloud.jobs.domain import job_timeout_seconds
from pocketpaw_ee.cloud.jobs.worker import execute_workspace_job
from pocketpaw_ee.cloud.metering.sweeper import sweep_unbilled_runs
from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db
from pocketpaw_ee.cloud.ship.deploy_job import deploy_app_job
from pocketpaw_ee.cloud.ship.job import provision_box_job
from pocketpaw_ee.sites.build_job import (
    ARQ_FUNCTION_NAME as SITE_BUILD_FUNCTION_NAME,
)
from pocketpaw_ee.sites.build_job import (
    PREVIEW_ARQ_FUNCTION_NAME as SITE_PREVIEW_BUILD_FUNCTION_NAME,
)
from pocketpaw_ee.sites.build_job import (
    run_site_build,
    run_site_preview_build,
    site_build_job_timeout_seconds,
    site_preview_job_timeout_seconds,
)
from pocketpaw_ee.sites.capacity import CAPACITY_MAX_TRIES

logger = logging.getLogger(__name__)


# A short cutoff because worker boot implies the previous worker just died;
# runs created seconds ago by the web process should not be swept.
_BOOT_SWEEP_OLDER_THAN_SECONDS = 5


def _boot_sweep_older_than_seconds() -> int:
    """The boot sweep's cutoff: short, but never inside the heartbeat interval.

    A running run is judged by its last heartbeat, which is up to one interval
    old on a perfectly healthy replica. A cutoff below that let a booting
    replica interrupt runs another replica was still driving. Three intervals
    leaves room for a missed beat.
    """
    return max(_BOOT_SWEEP_OLDER_THAN_SECONDS, int(3 * _heartbeat_seconds()))


# Default off — multi-replica safety. See module docstring.
_BOOT_SWEEP_ENV = "POCKETPAW_CLOUD_WORKER_BOOT_SWEEP"


def _boot_sweep_enabled() -> bool:
    return os.environ.get(_BOOT_SWEEP_ENV, "").strip().lower() == "true"


async def execute_run_job(ctx: dict[str, Any], spec_dict: dict[str, Any]) -> None:
    """arq job entrypoint — rehydrate the RunSpec and run the agent.

    A ``job_try`` past 1 is arq re-running a job a worker shutdown cancelled. It is
    driven only when the run doc was requeued (still ``queued``): a run that ended
    ``interrupted`` instead (it called tools, or used its attempts) already told its
    client so, and driving it again would answer nobody.
    """
    spec = RunSpec.model_validate(spec_dict)
    if (ctx.get("job_try") or 1) > 1:
        if not await run_service.is_queued(spec.run_id):
            logger.info("worker: run %s already ended; not retrying it", spec.run_id)
            return
        logger.info("worker: retrying run %s after a worker shutdown", spec.run_id)
        await execute_run(spec, retry=True)
    else:
        logger.info("worker: starting run %s", spec.run_id)
        await execute_run(spec)
    # Same as the in-process executor: charge at completion so the balance is not
    # a sweep interval behind. Idempotent, best-effort, sweeper still the backstop.
    from pocketpaw_ee.cloud.metering.service import bill_run_now

    await bill_run_now(spec.run_id)


async def _bootstrap(ctx: dict[str, Any]) -> None:
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
        swept = await sweep_stale_runs(older_than_seconds=_boot_sweep_older_than_seconds())
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


# TWO WorkerSettings classes now share this bootstrap inside ONE process: the chat
# lane below and the sites lane in ``pocketpaw_ee.sites.build_worker``, started
# together by ``pocketpaw_ee.cloud.worker_supervisor``. arq calls ``on_startup``
# once per Worker, so without a guard the second lane would re-run ``init_cloud_db``
# against a live client and re-run BOTH boot sweeps (compute-cost metering and the
# LiteLLM billing cutover). Those two are idempotent by ledger key, so a second pass
# would waste work rather than double-charge — but "probably harmless" is not the
# standard for a billing path, and a second ``init_cloud_db`` is not something to
# discover in production.
#
# The lock is held ACROSS the bootstrap, not just around the counter. A lane that
# arrives while the first is still initialising has to wait for the database it is
# about to run jobs against, and releasing the lock early would let it start empty.
_bootstrap_lock = asyncio.Lock()
_bootstrap_lanes = 0

# How long shutdown waits for cancelled runs' cleanups (partial Message, terminal
# status, ``interrupted`` frame) before closing the database under them. Bounded
# so a wedged write cannot hold a deploy; each cleanup is a handful of writes.
_CLEANUP_DRAIN_TIMEOUT_SECONDS = 10.0


async def _startup(ctx: dict[str, Any]) -> None:
    """Run :func:`_bootstrap` for the FIRST lane in this process only."""
    global _bootstrap_lanes
    async with _bootstrap_lock:
        _bootstrap_lanes += 1
        if _bootstrap_lanes > 1:
            logger.info(
                "worker boot: bootstrap already done in this process (lane %d)",
                _bootstrap_lanes,
            )
            return
        await _bootstrap(ctx)


async def _shutdown(ctx: dict[str, Any]) -> None:
    """Close the shared database once the LAST lane in this process has stopped."""
    global _bootstrap_lanes
    async with _bootstrap_lock:
        _bootstrap_lanes -= 1
        if _bootstrap_lanes > 0:
            return
        # Clamp rather than trust the count: an unbalanced shutdown (a lane whose
        # on_startup raised still gets its on_shutdown called) would otherwise drive
        # this negative and leave the NEXT bootstrap thinking a lane is still up.
        _bootstrap_lanes = 0
        # Let cancelled runs finish writing their partial reply and terminal
        # status first; closing the DB under them loses both.
        await drain_pending_cleanups(timeout=_CLEANUP_DRAIN_TIMEOUT_SECONDS)
        await close_cloud_db()


def _reset_bootstrap_for_tests() -> None:
    """Drop the lane count so a test can bootstrap again from scratch."""
    global _bootstrap_lanes
    _bootstrap_lanes = 0


# Public aliases so the sites lane can share this bootstrap without reaching across
# modules for a private name. One bootstrap per process is the contract; see the
# lane counter above for what happens without it.
worker_startup = _startup
worker_shutdown = _shutdown


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
#
# The resolver itself moved to ``runs/domain.py`` on 2026-09-04 so the SSE
# reader in ``router.py`` can share it: the stream's maximum lifetime is derived
# from this timeout, and a second copy of the parse would let the two drift.
# Re-exported under the old private names because they are the module's
# established surface and the worker config tests read them here.
_DEFAULT_RUN_JOB_TIMEOUT_SECONDS = DEFAULT_RUN_JOB_TIMEOUT_SECONDS
_job_timeout_seconds = run_job_timeout_seconds


# arq's own default is max_jobs=10 (arq/worker.py). That ceiling is shared by
# EVERY function registered below — chat runs, workspace jobs, both /ship jobs
# and both site builds — so ten concurrent site publishes leave no slot for a
# chat run. It is the cluster-wide concurrency limit, not a per-lane one:
# replicas multiply it, this value does not.
_DEFAULT_MAX_JOBS = 10


def _max_jobs() -> int:
    """Resolve the worker's concurrent-job ceiling from ``POCKETPAW_ARQ_MAX_JOBS``.

    Same fail-soft contract as ``_job_timeout_seconds``: an unparseable or
    non-positive value falls back to the default rather than being handed to
    arq, where ``0`` would wedge the worker into accepting nothing and a
    negative would crash ``BoundedSemaphore``.

    Raise this WITH the container's memory limit, not instead of it. The
    default ``claude_agent_sdk`` backend spawns a Node subprocess per run, so
    concurrency here is bounded by RAM long before it is bounded by CPU.
    """
    raw = os.environ.get("POCKETPAW_ARQ_MAX_JOBS", "").strip()
    if not raw:
        return _DEFAULT_MAX_JOBS
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "POCKETPAW_ARQ_MAX_JOBS=%r is not an int; using default %d",
            raw,
            _DEFAULT_MAX_JOBS,
        )
        return _DEFAULT_MAX_JOBS
    if val <= 0:
        logger.warning(
            "POCKETPAW_ARQ_MAX_JOBS=%d is not positive; using default %d",
            val,
            _DEFAULT_MAX_JOBS,
        )
        return _DEFAULT_MAX_JOBS
    return val


# arq writes a health-check key to Redis every ``health_check_interval`` seconds
# with a TTL of interval + 1, and ``arq --check <settings>`` exits 0 while that
# key is alive. arq's own default interval is 3600, which makes the key a poor
# liveness signal for anything that polls: a worker that died a minute ago still
# reports healthy for the rest of the hour. That is worse than no probe, because
# a container healthcheck built on it looks like coverage while catching nothing.
#
# 30 seconds costs one small Redis write every 30 seconds and makes the key mean
# what a probe assumes it means.
_DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS = 30


def _health_check_interval_seconds() -> int:
    """Resolve the arq health-key refresh period from the environment.

    Same fail-soft contract as ``_max_jobs`` and ``_job_timeout_seconds``: an
    unparseable or non-positive value falls back to the default rather than
    reaching arq, where a non-positive interval turns the health loop into a
    busy wait.

    Raising this loosens every probe built on the key, because the key's TTL is
    derived from it. Keep it comfortably below the healthcheck interval of
    whatever is polling, or the probe flaps.
    """
    raw = os.environ.get("POCKETPAW_ARQ_HEALTH_CHECK_INTERVAL", "").strip()
    if not raw:
        return _DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "POCKETPAW_ARQ_HEALTH_CHECK_INTERVAL=%r is not an int; using default %ds",
            raw,
            _DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
        )
        return _DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
    if val <= 0:
        logger.warning(
            "POCKETPAW_ARQ_HEALTH_CHECK_INTERVAL=%d is not positive; using default %ds",
            val,
            _DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
        )
        return _DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS
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

# The /ship box-provisioning job (SHIP-2) rides the same worker with its OWN
# timeout: provisioning is a poll-and-probe loop that can run for minutes while
# a fresh box boots and Dokku installs, so it must not be clipped by the
# chat-run timeout. The enqueue name is pinned to match ``ship/enqueue.py``.
# max_tries=1: the job never raises for an operational failure (it records the
# box ``degraded`` and returns), and its create step is idempotent on the stored
# server_id, so arq-level retries are neither needed nor wanted.
_ship_provision_fn = func(
    provision_box_job,
    name="provision_box_job",
    timeout=job_timeout_seconds(),
    max_tries=1,
)


# The /ship deploy job (SHIP-3) rides the same worker, wrapped like the
# provisioning job: its own long timeout (a deploy pulls an image and swaps
# containers, well past the chat-run budget) and max_tries=1 — the job records
# the attempt ``failed`` rather than raising, so an arq retry would only re-run
# a known-bad deploy. The enqueue name is pinned to match ``ship/enqueue.py``.
_ship_deploy_fn = func(
    deploy_app_job,
    name="deploy_app_job",
    timeout=job_timeout_seconds(),
    max_tries=1,
)


# SL-2: site builds ride this same worker with their OWN budget, for the reason in the
# module docstring — the in-sandbox ``timeout(1)`` must be the thing that fires first, or
# the lane loses the sentinel it classifies from. Evaluated at import, like the two
# timeouts above, so a deploy that retunes ``PAW_SITES_BUILD_TIMEOUT_SEC*`` picks it up
# on the worker restart the deploy performs anyway.
#
# ``max_tries`` admits ONLY the job's own capacity retries (``sites.capacity``): the job
# raises arq's ``Retry`` when Daytona refuses a sandbox because the org's resource limit
# is full, and nothing else in it raises ``Retry``. Every other outcome still settles on
# the first try, because a build is billed per attempt and the retry decision belongs to
# ``build_state.settle``, not to arq silently re-running a job whose row says ``failed``.
_site_build_fn = func(
    run_site_build,
    name=SITE_BUILD_FUNCTION_NAME,
    timeout=site_build_job_timeout_seconds(),
    max_tries=CAPACITY_MAX_TRIES,
)

# SP-2: the DRAFT-PREVIEW build. Same sandbox, same budget (it is the same build — only
# what happens to the artifact differs), and the same capacity-only ``max_tries``: a
# preview is billed per attempt too, and a client re-triggers by asking for the render
# again rather than by arq silently re-running a job whose result says why it failed.
# PP-2: plus the browser check that now runs in the same sandbox after the build.
_site_preview_build_fn = func(
    run_site_preview_build,
    name=SITE_PREVIEW_BUILD_FUNCTION_NAME,
    timeout=site_preview_job_timeout_seconds(),
    max_tries=CAPACITY_MAX_TRIES,
)


# The one function arq may retry: a chat run the worker cancelled while shutting down
# requeues itself, and arq only re-runs a cancelled job while ``job_try <= max_tries``.
# The name is pinned to what ``ArqExecutor`` enqueues and what the supervisor's
# interactive-job exclusion matches.
_execute_run_fn = func(execute_run_job, name="execute_run_job", max_tries=MAX_RUN_ATTEMPTS)


class WorkerSettings:
    """arq worker configuration. Loaded by ``arq <dotted-path>``."""

    functions = [
        _execute_run_fn,
        _workspace_job_fn,
        _ship_provision_fn,
        _ship_deploy_fn,
        _site_build_fn,
        _site_preview_build_fn,
    ]
    on_startup = _startup
    on_shutdown = _shutdown
    # Crash policy: no auto-retry. A failed run is left as ``failed``/``interrupted``
    # so the user can decide whether to resend — re-running could double-bill or
    # surface a partial duplicate. ``_execute_run_fn`` overrides this for the one
    # retry a worker shutdown earns (see the module docstring).
    max_tries = 1
    # Per-run timeout. arq's default (300s) cancels long agent runs mid-stream; lift
    # it and make it env-tunable (POCKETPAW_CLOUD_RUN_JOB_TIMEOUT, default 30 min).
    # Plain int in __dict__ for the same arq-reads-__dict__ reason as redis_settings.
    job_timeout = _job_timeout_seconds()
    # Concurrent-job ceiling, shared across every lane in ``functions``. arq's
    # default of 10 is the first limit a multi-user deploy hits: job 11 waits in
    # Redis behind a 30-minute ``job_timeout`` with ``max_tries=1``, so it is not
    # retried, just queued. Plain int in __dict__ for the arq-reads-__dict__
    # reason documented on `_redis_settings`.
    max_jobs = _max_jobs()
    # How often the worker refreshes its Redis health key, and therefore how
    # quickly ``arq --check`` notices it is gone. arq's default of an hour makes
    # that check answer "healthy" for up to an hour after the process died,
    # which is exactly the wrong answer to give a container healthcheck. Plain
    # int in __dict__ for the arq-reads-__dict__ reason above.
    health_check_interval = _health_check_interval_seconds()
    # Eager: arq reads __dict__, which bypasses descriptors. See `_redis_settings`.
    redis_settings = _redis_settings()


# Shared with the sites lane (``pocketpaw_ee.sites.build_worker``). Aliases rather
# than a second definition: one copy of the site-build timeout, one copy of the
# health-key period, one Redis resolver. A duplicated build timeout is precisely the
# drift that would let arq cancel a build before its in-sandbox ``timeout(1)`` fires,
# and that destroys the sentinel the lane classifies its verdict from — a healthy
# but slow build would be recorded as lost infrastructure.
site_build_fn = _site_build_fn
site_preview_build_fn = _site_preview_build_fn
arq_redis_settings = _redis_settings
arq_health_check_interval_seconds = _health_check_interval_seconds
