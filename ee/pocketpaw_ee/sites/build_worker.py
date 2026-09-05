# ee/pocketpaw_ee/sites/build_worker.py — the site-build lane's OWN arq worker settings.
#
# Created 2026-09-04 (fix/queue-lanes, backend-perf C1). Until now every background lane
# this product runs — chat runs, workspace jobs, both /ship jobs and both site builds —
# shared arq's default queue, and therefore shared ONE ``max_jobs`` ceiling whose default
# is 10. That is a cluster-wide number, not a per-lane one, and site builds are the worst
# possible neighbour to share it with: a publish storm is one customer pressing Publish on
# ten sites, and a single build's budget is 1020s at today's defaults. Ten of them left
# chat with zero slots, and the eleventh request did not fail — it sat in Redis until the
# stale-run sweeper marked it interrupted ten minutes later, while the user watched an SSE
# stream deliver nothing but heartbeats.
#
# Splitting the queue is what fixes that, and a queue without a consumer is worse than no
# split at all, so this module exists to BE that consumer. It is deliberately thin: every
# job function, the bootstrap, the Redis resolver and the health-key period are the same
# objects the chat lane uses, imported rather than redefined. The only thing this lane
# owns is its own concurrency ceiling.
#
# HOW IT RUNS. ``pocketpaw_ee.cloud.worker_supervisor`` starts this lane and the chat lane
# as two arq Workers in ONE process, because the Coolify compose file cannot take a third
# service that declares ``build:`` (Coolify base64s the Dockerfile into one SSH argv per
# build service; a third one blew the argv limit and broke every deploy — paw-workspace
# #193, reverted in #194). Running it standalone also works and needs no code change::
#
#     arq pocketpaw_ee.sites.build_worker.WorkerSettings
#
# WHY 4 AND NOT 10. A chat run spawns a Node subprocess per run on the default
# ``claude_agent_sdk`` backend, so the chat lane's ceiling is bounded by container RAM. A
# site build is not: the compile happens in a Daytona sandbox and what runs here is an
# await on a remote exec. So these four slots cost the worker container almost nothing,
# and the number is about how many sandboxes the account should be paying for at once
# rather than about memory. Raise it with the Daytona quota, not with the memory limit.
"""The site-build lane's arq WorkerSettings (backend-perf C1)."""

from __future__ import annotations

import logging
import os

from pocketpaw_ee.cloud.chat.runs.worker import (
    arq_health_check_interval_seconds,
    arq_redis_settings,
    site_build_fn,
    site_preview_build_fn,
    worker_shutdown,
    worker_startup,
)
from pocketpaw_ee.sites.build_job import SITE_BUILD_QUEUE_NAME, site_build_job_timeout_seconds

logger = logging.getLogger(__name__)

#: Concurrent site builds per worker process. See the header for why this is small and
#: what it is actually bounded by.
_DEFAULT_SITES_MAX_JOBS = 4

_MAX_JOBS_ENV = "POCKETPAW_SITES_ARQ_MAX_JOBS"


def _sites_max_jobs() -> int:
    """Resolve this lane's concurrent-job ceiling from the environment.

    Same fail-soft contract as the chat lane's ``_max_jobs``: an unparseable or
    non-positive value falls back to the default rather than reaching arq, where ``0``
    wedges the worker into accepting nothing and a negative crashes ``BoundedSemaphore``.

    It reads its OWN variable rather than ``POCKETPAW_ARQ_MAX_JOBS``. Sharing one would
    undo the split: raising the chat ceiling to survive a busy morning would raise the
    build ceiling with it, and the point of the lane is that those two numbers answer to
    different limits (container RAM here, the sandbox quota there).
    """
    raw = os.environ.get(_MAX_JOBS_ENV, "").strip()
    if not raw:
        return _DEFAULT_SITES_MAX_JOBS
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not an int; using default %d", _MAX_JOBS_ENV, raw, _DEFAULT_SITES_MAX_JOBS
        )
        return _DEFAULT_SITES_MAX_JOBS
    if val <= 0:
        logger.warning(
            "%s=%d is not positive; using default %d", _MAX_JOBS_ENV, val, _DEFAULT_SITES_MAX_JOBS
        )
        return _DEFAULT_SITES_MAX_JOBS
    return val


class WorkerSettings:
    """arq worker configuration for the site-build queue."""

    queue_name = SITE_BUILD_QUEUE_NAME
    # The SAME wrapped function objects the chat lane registers, not copies. Each already
    # carries its own timeout and ``max_tries=1``; re-wrapping them here would fork the
    # build timeout, and a build that arq cancels before its in-sandbox timeout fires is
    # recorded as lost infrastructure rather than as the slow-but-healthy build it was.
    functions = [site_build_fn, site_preview_build_fn]
    on_startup = worker_startup
    on_shutdown = worker_shutdown
    # No auto-retry, matching every other lane: a build is billed per attempt in a
    # third-party sandbox, and the retry decision belongs to ``build_state.settle``, which
    # records WHY it gave up, not to arq silently re-running a job whose row says failed.
    max_tries = 1
    # This lane's own ceiling — the whole point of the split. Plain int in ``__dict__``
    # because arq's ``worker.get_kwargs`` reads ``settings_cls.__dict__`` directly, which
    # bypasses the descriptor protocol and would hand a property object to the Worker.
    max_jobs = _sites_max_jobs()
    # Both wrapped functions carry their own per-function timeout, which is what actually
    # governs a build. This class-level value exists so the fallback is not arq's 300s
    # default, which would be under a third of a build's budget.
    job_timeout = site_build_job_timeout_seconds()
    # Same health-key period as the chat lane, so ``arq --check`` against THIS settings
    # class is as truthful as it is against that one. Eager for the __dict__ reason above.
    health_check_interval = arq_health_check_interval_seconds()
    # Eager for the __dict__ reason above.
    redis_settings = arq_redis_settings()
