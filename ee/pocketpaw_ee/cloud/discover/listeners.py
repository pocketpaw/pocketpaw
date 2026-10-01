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

from __future__ import annotations

import logging

from pocketpaw_ee.cloud._core.realtime.bus import get_bus
from pocketpaw_ee.cloud._core.realtime.events import (
    Event,
    SiteTemplateDeleted,
    SiteTemplateSaved,
    SiteTemplateUpdated,
)
from pocketpaw_ee.cloud.discover import service_admin

logger = logging.getLogger(__name__)


async def on_site_template_changed(event: Event) -> None:
    """Sync the template named by ``event.data["id"]`` into Discover."""
    data = getattr(event, "data", None) or {}
    template_id = data.get("id")
    if not template_id:
        return
    try:
        await service_admin.sync_site_template(str(template_id))
    except Exception:
        logger.exception("discover: sync of site template %s failed", template_id)


def register_discover_listeners() -> None:
    """Subscribe the site-template sync. Called once from ``mount_cloud``."""
    bus = get_bus()
    for event_cls in (SiteTemplateSaved, SiteTemplateUpdated, SiteTemplateDeleted):
        bus.subscribe(event_cls.EVENT_TYPE, on_site_template_changed)


__all__ = ["on_site_template_changed", "register_discover_listeners"]
