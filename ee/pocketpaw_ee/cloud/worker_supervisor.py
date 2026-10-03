# ee/pocketpaw_ee/cloud/worker_supervisor.py — runs every arq lane in ONE process.
#
# A queue with no consumer is worse than a shared one, because the job waits forever and
# nothing says so. This module is the consumer side: it starts the chat lane, the
# site-build lane and, when POCKETPAW_GROWTH_WORKER_ENABLED is on, the growth lane, as
# arq Workers on one event loop.
#
# WHY ONE PROCESS AND NOT TWO CONTAINERS. Coolify ships the Dockerfile to the deploy host
# by base64-encoding it into a single SSH command line, ONCE PER COMPOSE SERVICE THAT
# DECLARES ``build:``. deploy/coolify/Dockerfile is ~24 KB, so each build service adds
# ~32 KB to one argv. Two services is ~63 KB of a ~128 KB budget; a third one broke every
# deploy with ``posix_spawn() failed: Argument list too long``, raised by Coolify's PHP
# before Docker ran at all, naming neither the service nor compose (paw-workspace #193,
# reverted in #194). So a new lane cannot be a new service, and this is the supported
# alternative: one container, one command, lanes with independent ceilings.
#
# WHY NOT ``sh -c 'arq A & exec arq B'``. Backgrounding the first lane makes its death
# invisible: the container stays up and healthy while site builds silently stop being
# consumed, which is the same class of failure the split was meant to remove. This
# supervisor treats ANY lane stopping as fatal to the process, so the container exits and
# the restart policy brings both lanes back together.
#
# PAW-LENS. ``_monitored`` wraps every lane's job and cron functions (names unchanged,
# so enqueuers still match) in ``automation_run("job", <name>)``: one check-in pair and
# ``paw.automation.*`` span attributes per job. Interactive chat runs are excluded — they are
# not automations, and ``execute_run`` stamps their workspace itself.
#
# EXIT CODES. 0 only when a signal asked us to stop. Non-zero when a lane ended on its
# own, whether it raised or returned — an arq Worker's ``async_run`` is not supposed to
# return while the process is meant to be serving, so a clean return is just as wrong as
# an exception and must not be reported as success.
"""Run every arq lane this deployment needs in a single process."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import signal
import sys
from typing import Any

from arq.constants import default_queue_name
from arq.worker import create_worker, func

from pocketpaw.lens_checkins import automation_run
from pocketpaw.lens_checkins import flush as flush_lens_checkins

logger = logging.getLogger(__name__)

#: Signals a container runtime uses to ask for a graceful stop. SIGINT is here for the
#: interactive case; SIGTERM is the one Docker actually sends.
_STOP_SIGNALS = ("SIGTERM", "SIGINT")


def default_lanes() -> list[type]:
    """The lanes a deployed worker container runs.

    Imported lazily so that importing this module does not drag in the whole cloud
    package — the settings classes evaluate their Redis config at class-body time and
    raise when ``POCKETPAW_REDIS_URL`` is unset, which is correct for a worker process
    and wrong for anything that merely imports the supervisor to inspect it.

    The growth lane (``pocketpaw_ee.cloud.growth.worker``) runs only when
    ``POCKETPAW_GROWTH_WORKER_ENABLED`` is on. It executes live outbound sends (email,
    WhatsApp) and the daily follow-up cron, so it is opt-in. The same flag lets the web
    process enqueue growth sends (``growth.executor``), so a deployment that sets it on
    the backend must set it on the worker too, or approved sends queue with no consumer.
    """
    from pocketpaw_ee.cloud.chat.runs.worker import WorkerSettings as ChatWorkerSettings
    from pocketpaw_ee.cloud.growth.executor import _growth_worker_enabled
    from pocketpaw_ee.sites.build_worker import WorkerSettings as SiteBuildWorkerSettings

    lanes: list[type] = [ChatWorkerSettings, SiteBuildWorkerSettings]
    if _growth_worker_enabled():
        from pocketpaw_ee.cloud.growth.worker import WorkerSettings as GrowthWorkerSettings

        lanes.append(GrowthWorkerSettings)
    return lanes


def lane_name(settings_cls: type) -> str:
    """A log-friendly name for a lane: the queue it reads.

    Falls back to arq's own default queue rather than to the class name, because every
    settings class in this codebase is called ``WorkerSettings`` — a log line naming the
    class would identify nothing, while the queue is exactly what distinguishes them.
    """
    queue = getattr(settings_cls, "queue_name", None)
    return str(queue) if queue else default_queue_name


def _install_stop_handlers(stop: asyncio.Event) -> None:
    """Ask the loop to set ``stop`` on SIGTERM / SIGINT.

    Best-effort by design. ``add_signal_handler`` is not implemented on Windows, and a
    supervisor that refused to start there would make the lanes untestable on a developer
    machine for no production benefit — the deployed container is Linux.
    """
    loop = asyncio.get_running_loop()
    for name in _STOP_SIGNALS:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):
            logger.debug("supervisor: no loop signal handler for %s on this platform", name)


async def _close_quietly(worker: Any, name: str) -> None:
    """Close a worker without letting shutdown raise.

    ``Worker.close`` calls ``handle_sig(signal.SIGUSR1)`` when it was built with
    ``handle_signals=False``, and SIGUSR1 does not exist on Windows — so this raises on a
    developer machine for a reason that has nothing to do with the code under test. It can
    also raise against a Redis that has already gone away, which is exactly when we are
    least able to do anything about it.
    """
    try:
        await worker.close()
    except Exception:
        logger.warning("supervisor: closing lane %s failed", name, exc_info=True)


#: Job names that are interactive runs, not automations; left unmonitored.
_INTERACTIVE_JOBS = frozenset({"execute_run_job"})


def _wrap(name: str, coroutine: Any) -> Any:
    async def _job(ctx: Any, *args: Any, **kwargs: Any) -> Any:
        async with automation_run("job", name):
            return await coroutine(ctx, *args, **kwargs)

    _job.__lens_monitor__ = ("job", name)  # type: ignore[attr-defined]
    return _job


def _monitored(settings_cls: type) -> dict[str, Any]:
    """``create_worker`` overrides: the lane's functions and cron jobs, check-in wrapped."""
    out: dict[str, Any] = {}
    fns = [func(entry) for entry in getattr(settings_cls, "functions", None) or []]
    if fns:
        out["functions"] = [
            f
            if f.name in _INTERACTIVE_JOBS
            else dataclasses.replace(f, coroutine=_wrap(f.name, f.coroutine))
            for f in fns
        ]
    crons = getattr(settings_cls, "cron_jobs", None) or []
    if crons:
        out["cron_jobs"] = [
            dataclasses.replace(c, coroutine=_wrap(c.name, c.coroutine)) for c in crons
        ]
    return out


async def run_lanes(settings_classes: list[type] | None = None) -> int:
    """Run every lane until one stops or a stop signal arrives. Returns an exit code."""
    classes = list(default_lanes() if settings_classes is None else settings_classes)
    if not classes:
        raise RuntimeError("worker supervisor started with no lanes to run")

    # ``handle_signals=False`` because each arq Worker would otherwise install its OWN
    # SIGTERM handler on the shared loop, where the last one registered silently replaces
    # every earlier one. The supervisor owns the signal and stops the lanes itself.
    workers = [create_worker(cls, handle_signals=False, **_monitored(cls)) for cls in classes]
    names = [lane_name(cls) for cls in classes]
    logger.info("supervisor: starting %d lane(s): %s", len(names), ", ".join(names))

    stop = asyncio.Event()
    _install_stop_handlers(stop)

    lane_tasks = [
        asyncio.create_task(worker.async_run(), name=f"lane:{name}")
        for worker, name in zip(workers, names, strict=True)
    ]
    stop_task = asyncio.create_task(stop.wait(), name="supervisor:stop")

    try:
        await asyncio.wait([*lane_tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)

        exit_code = 0
        for task, name in zip(lane_tasks, names, strict=True):
            if not task.done():
                continue
            exit_code = 1
            exc = task.exception() if not task.cancelled() else None
            if exc is not None:
                logger.error("supervisor: lane %s raised; stopping the process", name, exc_info=exc)
            else:
                logger.error("supervisor: lane %s returned on its own; stopping the process", name)
        if exit_code == 0:
            logger.info("supervisor: stop requested; draining %d lane(s)", len(names))
        return exit_code
    finally:
        stop_task.cancel()
        for task in lane_tasks:
            task.cancel()
        # Gather before closing: a cancelled lane task still has to finish unwinding
        # before its Worker's own cleanup can run against the same Redis pool.
        await asyncio.gather(*lane_tasks, stop_task, return_exceptions=True)
        for worker, name in zip(workers, names, strict=True):
            await _close_quietly(worker, name)
        # Deliver the last paw-lens check-ins; 1 s budget, never raises.
        await flush_lens_checkins(timeout=1.0)


def main() -> int:
    # setup_logging, not basicConfig: the worker is where money is spent and
    # where provider SDKs surface credentials in exception text, and only
    # setup_logging installs the secret / PII scrubbing filters. basicConfig
    # gives the root logger a handler and nothing else, so the worker was
    # logging unredacted to stdout where the platform's collector keeps it.
    from pocketpaw.logging_setup import setup_logging

    setup_logging(level=os.environ.get("POCKETPAW_LOG_LEVEL", "INFO"))
    return asyncio.run(run_lanes())


if __name__ == "__main__":
    sys.exit(main())
