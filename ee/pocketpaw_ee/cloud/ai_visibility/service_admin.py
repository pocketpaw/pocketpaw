# AI visibility — cross-tenant reads for the monthly sweep.
#
# ``sweep_due_checks`` queues a check for every site whose owner has saved
# questions, whose last check was queued 30 or more days ago (or never), and whose
# plan still sells the concierge (Staff). Runs from the daily arq cron in
# ``worker.py``; a daily tick over a 30-day window means a site is checked about
# 30 days after its last check, not on a calendar day. Queuing stamps
# ``requested_at`` first (``service.queue_check``), so a second tick never queues
# a site twice. With no engine configured it queues nothing.

from __future__ import annotations

import logging
from datetime import UTC, datetime

from pocketpaw_ee.cloud.ai_visibility import service
from pocketpaw_ee.cloud.ai_visibility.engines import default_engines
from pocketpaw_ee.cloud.models.ai_visibility_site import AiVisibilitySite
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

logger = logging.getLogger(__name__)


async def due_site_checks(now: datetime) -> list[AiVisibilitySite]:
    # admin-cross-tenant: the monthly sweep looks across every workspace for Staff
    # sites due a check; each site is re-read scoped to its row's workspace.
    cutoff = now - service.CHECK_INTERVAL
    states = await AiVisibilitySite.find(
        {
            "questions": {"$ne": []},
            "$or": [{"requested_at": None}, {"requested_at": {"$lt": cutoff}}],
        }
    ).to_list()
    due = []
    for state in states:
        oid = service._oid(state.site_id)
        site = await _SiteDoc.find_one({"_id": oid, "workspace": state.workspace}) if oid else None
        if site is not None and service.site_plan_allows_check(site):
            due.append(state)
    return due


async def sweep_due_checks(now: datetime | None = None) -> int:
    # admin-cross-tenant: queues checks for due sites in every workspace.
    if not default_engines():
        logger.info("ai_visibility: monthly sweep skipped, no engine configured")
        return 0
    now = now or datetime.now(UTC)
    queued = 0
    for state in await due_site_checks(now):
        try:
            await service.queue_check(state, now)
            queued += 1
        except Exception:
            logger.exception("ai_visibility: could not queue the check for %s", state.site_id)
    logger.info("ai_visibility: monthly sweep queued %d check(s)", queued)
    return queued


__all__ = ["due_site_checks", "sweep_due_checks"]
