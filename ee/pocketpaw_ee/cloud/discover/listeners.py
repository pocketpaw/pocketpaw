# Discover — in-process bus subscribers that keep listings in sync with sources.
#
# Created 2026-10-01 (feat/discover-index). Site templates emit
# ``site_template.saved`` / ``.updated`` / ``.deleted`` (an update also covers a
# report-triggered hide). Each event re-reads the template by id through
# ``site_templates.service_admin`` (never trusting the payload) and lists it if
# it is public and unhidden, else removes its listing. ``site_templates`` never
# imports discover: the dependency points this way only. Wired from
# ``mount_cloud`` after ``init_realtime``. A failing sync is logged and
# swallowed so one bad event cannot break the bus; ``reindex`` repairs drift.
#
# Updated 2026-10-02 (feat/discover-index, hardening): ``start_discover_reindex``
# / ``stop_discover_reindex`` run ``reindex("site_template")`` every 30 minutes
# (missed events, stale ``live_url``). Wired in ``mount_cloud`` behind
# ``POCKETPAW_CLOUD_SCHEDULER_ENABLED`` and a ``leased`` lock like the other loops.
#
# Updated 2026-10-02 (feat/discover-index, review): the loop runs its first pass
# at once (pass, then sleep), so templates that were public before a deploy are
# listed on boot. With the scheduler flag off, ``start_discover_backfill`` runs
# ONE background pass at startup (fire-and-forget, logged on failure);
# ``stop_discover_backfill`` cancels it if it is still running at shutdown.
#
# Updated 2026-10-02 (feat/discover-source-contract): site-template events call
# ``service_admin.sync_source("site_template", id)``. The periodic loop and the
# startup backfill reindex every registered source that has ``iter_public``; a
# failing source is logged and the others still run.
#
# Updated 2026-10-02 (feat/studio-templates): ``studio_template.saved`` /
# ``.updated`` / ``.deleted`` call ``sync_source("studio_template", id)`` the
# same way (logged and swallowed on failure).

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from pocketpaw_ee.cloud._core.realtime.bus import get_bus
from pocketpaw_ee.cloud._core.realtime.events import (
    Event,
    SiteTemplateDeleted,
    SiteTemplateSaved,
    SiteTemplateUpdated,
    StudioTemplateDeleted,
    StudioTemplateSaved,
    StudioTemplateUpdated,
)
from pocketpaw_ee.cloud.discover import service_admin
from pocketpaw_ee.cloud.discover.sources import registered_sources

logger = logging.getLogger(__name__)

REINDEX_INTERVAL_SECONDS = 30 * 60
_REINDEX_TASK_KEY = "discover_reindex_task"
_BACKFILL_TASK_KEY = "discover_backfill_task"


async def on_site_template_changed(event: Event) -> None:
    """Sync the template named by ``event.data["id"]`` into Discover."""
    data = getattr(event, "data", None) or {}
    template_id = data.get("id")
    if not template_id:
        return
    try:
        await service_admin.sync_source(service_admin.SITE_TEMPLATE, str(template_id))
    except Exception:
        logger.exception("discover: sync of site template %s failed", template_id)


async def on_studio_template_changed(event: Event) -> None:
    """Sync the studio template named by ``event.data["id"]`` into Discover."""
    data = getattr(event, "data", None) or {}
    template_id = data.get("id")
    if not template_id:
        return
    try:
        await service_admin.sync_source(service_admin.STUDIO_TEMPLATE, str(template_id))
    except Exception:
        logger.exception("discover: sync of studio template %s failed", template_id)


def register_discover_listeners() -> None:
    """Subscribe the site- and studio-template syncs. Called once from ``mount_cloud``."""
    bus = get_bus()
    for event_cls in (SiteTemplateSaved, SiteTemplateUpdated, SiteTemplateDeleted):
        bus.subscribe(event_cls.EVENT_TYPE, on_site_template_changed)
    for event_cls in (StudioTemplateSaved, StudioTemplateUpdated, StudioTemplateDeleted):
        bus.subscribe(event_cls.EVENT_TYPE, on_studio_template_changed)


async def _reindex_once() -> None:
    """One reindex pass over every registered source that can reindex. A
    failing source is logged, never raised, and the rest still run;
    ``CancelledError`` propagates for a clean shutdown."""
    for src in registered_sources():
        if src.iter_public is None:
            continue
        try:
            result = await service_admin.reindex(src.name)
            logger.info("discover: reindex %s", result)
        except Exception:
            logger.exception("discover: reindex failed for %s", src.name)


async def _run_reindex_loop() -> None:
    """Reindex, sleep, repeat. The first pass runs at once so a fresh deploy
    lists templates that were already public."""
    while True:
        await _reindex_once()
        await asyncio.sleep(REINDEX_INTERVAL_SECONDS)


async def start_discover_backfill(app: Any) -> None:
    """Without the scheduler: run one reindex pass in the background and return
    at once (off the startup path). Idempotent, so several processes doing it
    is harmless."""
    task = asyncio.create_task(_reindex_once(), name="discover-backfill")
    setattr(app.state, _BACKFILL_TASK_KEY, task)  # hold a reference: no GC


async def stop_discover_backfill(app: Any) -> None:
    """Cancel the startup backfill if it is still running."""
    task = getattr(app.state, _BACKFILL_TASK_KEY, None)
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def start_discover_reindex(app: Any) -> None:
    """Start the periodic reindex. A second start is a no-op."""
    existing = getattr(app.state, _REINDEX_TASK_KEY, None)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_run_reindex_loop(), name="discover-reindex")
    setattr(app.state, _REINDEX_TASK_KEY, task)


async def stop_discover_reindex(app: Any) -> None:
    """Cancel and await the reindex loop. Safe to call more than once."""
    task = getattr(app.state, _REINDEX_TASK_KEY, None)
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task
    setattr(app.state, _REINDEX_TASK_KEY, None)


__all__ = [
    "on_site_template_changed",
    "on_studio_template_changed",
    "register_discover_listeners",
    "start_discover_backfill",
    "start_discover_reindex",
    "stop_discover_backfill",
    "stop_discover_reindex",
]
