# ee/pocketpaw_ee/cloud/jobs/plugins.py
# Created: 2026-06-22 (feat/jobs-custom-job-entrypoints) — the SAFE
# entry-point discovery path for WORKSPACE-CUSTOM jobs. A deploy/workspace
# ships a custom job by declaring an entry-point in the new
# ``pocketpaw.jobs`` group; ``load_entrypoint_jobs()`` discovers those
# entry-points at process startup, loads each one, and registers the
# resolved ``JobCallable``(s) into the same process-wide registry the
# built-ins use. No runtime ``import`` of user-supplied code paths —
# discovery is via installed-package metadata only, mirroring how the OSS
# core (``pocketpaw._registry``) finds its optional providers and degrades
# gracefully when none are installed. This module owns the discovery logic
# so ``registry.py`` can stay a pure, dependency-free registry contract.

"""Entry-point discovery + registration for workspace-custom jobs.

A custom job package declares::

    [project.entry-points."pocketpaw.jobs"]
    my_jobs = "my_pkg:make_jobs"

where ``make_jobs`` is a zero-arg factory returning a single
:class:`~pocketpaw_ee.cloud.jobs.registry.JobCallable` or an iterable of
them. :func:`load_entrypoint_jobs` is called once per process at startup
(right after ``register_builtins()``) by BOTH the web app (``mount_cloud``)
and the ARQ worker (``_startup``), so a custom job resolves the same way on
either side of the dispatch boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from importlib.metadata import entry_points

from pocketpaw_ee.cloud.jobs.registry import JobCallable, register_job

logger = logging.getLogger(__name__)

# The entry-point group a workspace/deploy declares a custom job under. The
# OSS core ships no entry-points of its own; this repo doesn't either — the
# group exists purely for downstream packages to extend (see docs/wiki/jobs.md).
JOBS_ENTRYPOINT_GROUP = "pocketpaw.jobs"


def _coerce_to_jobs(resolved: object) -> list[JobCallable]:
    """Normalise a factory's return value into a list of candidate jobs.

    The contract is "a zero-arg factory returning a ``JobCallable`` OR an
    iterable of them". A bare callable (which a single ``JobCallable``
    instance is) must NOT be iterated, so we special-case it before the
    generic iterable branch. Strings/bytes are never valid job containers.
    """
    if isinstance(resolved, (str, bytes)):
        return []
    if isinstance(resolved, Iterable):
        return list(resolved)
    return [resolved]


def load_entrypoint_jobs() -> int:
    """Discover + register workspace-custom jobs from installed entry-points.

    Discovers every entry-point in the ``pocketpaw.jobs`` group, calls each
    (the entry-point resolves to a zero-arg factory), and registers each
    resolved :class:`JobCallable` into the process-wide registry via
    :func:`register_job`.

    Safety + resilience properties:

    - **No-op when none are installed.** An OSS / no-plugin install finds no
      entry-points and returns ``0`` with no error — same graceful degradation
      as ``pocketpaw._registry``.
    - **A bad provider never crashes startup.** If an entry-point fails to load,
      its factory raises, or a resolved object doesn't satisfy the
      :class:`JobCallable` protocol (missing ``name`` / async ``__call__``), it
      is skipped with a logged warning and the next provider still loads.
    - **Idempotent.** Safe to call once per process; re-registering an existing
      name just overwrites (last-writer-wins, same as the built-ins).

    Returns the number of jobs successfully registered (handy for logging /
    tests).
    """
    registered = 0
    for ep in entry_points(group=JOBS_ENTRYPOINT_GROUP):
        try:
            factory = ep.load()
        except Exception as exc:  # noqa: BLE001 — isolate one bad plugin
            logger.warning("workspace-custom job entry-point %r failed to load: %s", ep.name, exc)
            continue

        try:
            resolved = factory()
        except Exception as exc:  # noqa: BLE001 — isolate a bad factory
            logger.warning("workspace-custom job factory %r raised on call: %s", ep.name, exc)
            continue

        for job in _coerce_to_jobs(resolved):
            if not isinstance(job, JobCallable):
                logger.warning(
                    "workspace-custom job from entry-point %r is not a JobCallable "
                    "(needs a `name` attribute and an async `__call__`) — skipping: %r",
                    ep.name,
                    job,
                )
                continue
            try:
                register_job(job)
            except Exception as exc:  # noqa: BLE001 — a nameless/invalid job
                logger.warning(
                    "workspace-custom job from entry-point %r failed to register: %s",
                    ep.name,
                    exc,
                )
                continue
            registered += 1
            logger.info(
                "registered workspace-custom job %r from entry-point %r",
                getattr(job, "name", "?"),
                ep.name,
            )

    return registered


__all__ = ["JOBS_ENTRYPOINT_GROUP", "load_entrypoint_jobs"]
