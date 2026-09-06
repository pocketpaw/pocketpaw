# ee/pocketpaw_ee/cloud/worker_supervisor.py — runs every arq lane in ONE process.
#
# Created 2026-09-04 (fix/queue-lanes, backend-perf C1). Splitting site builds onto their
# own queue is only half the fix; a queue with no consumer is worse than a shared one,
# because the job waits forever and nothing says so. This module is the consumer side:
# it starts the chat lane and the site-build lane as two arq Workers on one event loop.
#
# WHY ONE PROCESS AND NOT TWO CONTAINERS. Coolify ships the Dockerfile to the deploy host
# by base64-encoding it into a single SSH command line, ONCE PER COMPOSE SERVICE THAT
# DECLARES ``build:``. deploy/coolify/Dockerfile is ~24 KB, so each build service adds
# ~32 KB to one argv. Two services is ~63 KB of a ~128 KB budget; a third one broke every
# deploy with ``posix_spawn() failed: Argument list too long``, raised by Coolify's PHP
# before Docker ran at all, naming neither the service nor compose (paw-workspace #193,
# reverted in #194). So a new lane cannot be a new service, and this is the supported
# alternative: one container, one command, two lanes with independent ceilings.
#
# WHY NOT ``sh -c 'arq A & exec arq B'``. Backgrounding the first lane makes its death
# invisible: the container stays up and healthy while site builds silently stop being
# consumed, which is the same class of failure the split was meant to remove. This
# supervisor treats ANY lane stopping as fatal to the process, so the container exits and
# the restart policy brings both lanes back together.
#
# EXIT CODES. 0 only when a signal asked us to stop. Non-zero when a lane ended on its
# own, whether it raised or returned — an arq Worker's ``async_run`` is not supposed to
# return while the process is meant to be serving, so a clean return is just as wrong as
# an exception and must not be reported as success.
"""Run every arq lane this deployment needs in a single process."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Any

from arq.constants import default_queue_name
from arq.worker import create_worker

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

    The growth lane (``pocketpaw_ee.cloud.growth.worker``) is deliberately NOT here. It
    has its own queue already and no consumer in the deployed compose file, so adding it
    would not be a refactor — it would start executing outbound work that is currently
    inert. That is a product decision, not a performance one.
    """
    from pocketpaw_ee.cloud.chat.runs.worker import WorkerSettings as ChatWorkerSettings
    from pocketpaw_ee.sites.build_worker import WorkerSettings as SiteBuildWorkerSettings

    return [ChatWorkerSettings, SiteBuildWorkerSettings]


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


async def run_lanes(settings_classes: list[type] | None = None) -> int:
    """Run every lane until one stops or a stop signal arrives. Returns an exit code."""
    classes = list(default_lanes() if settings_classes is None else settings_classes)
    if not classes:
        raise RuntimeError("worker supervisor started with no lanes to run")

    # ``handle_signals=False`` because each arq Worker would otherwise install its OWN
    # SIGTERM handler on the shared loop, where the last one registered silently replaces
    # every earlier one. The supervisor owns the signal and stops the lanes itself.
    workers = [create_worker(cls, handle_signals=False) for cls in classes]
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


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    return asyncio.run(run_lanes())


if __name__ == "__main__":
    sys.exit(main())
