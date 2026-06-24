# ee/pocketpaw_ee/cloud/jobs/builtin/__init__.py
# Created: 2026-06-20 (feat/workspace-jobs, pp#1459) — the built-in jobs
# package + the `register_builtins()` entry point `mount_cloud` calls AFTER
# `init_realtime()`. Built-ins are registered into the process-wide registry
# at mount time (the same lifecycle the outcomes-ledger / upload listeners
# use). Adding a new built-in = add it to the list here.

"""Built-in workspace jobs + their mount-time registration."""

from __future__ import annotations

from pocketpaw_ee.cloud.jobs.builtin.score_applications import ScoreApplicationsJob
from pocketpaw_ee.cloud.jobs.registry import register_job

# The built-ins to register at mount time. Instantiated once; jobs are
# stateless callables so a single instance is reused for every run.
_BUILTINS = (ScoreApplicationsJob(),)


def register_builtins() -> None:
    """Register every built-in job into the process-wide registry.

    Called once from ``mount_cloud`` after ``init_realtime``. Idempotent —
    re-registering a name overwrites with the same callable, so a test re-mount
    is harmless.
    """
    for job in _BUILTINS:
        register_job(job)


__all__ = ["register_builtins"]
