# ee/pocketpaw_ee/site_ai/alerts.py — tell the owner when a site's AI spend crosses a
# threshold of its monthly cap.
#
# After each settle, ``check`` compares the month's settled spend with the
# effective monthly cap (owner cap, bounded by the plan allowance). For the highest
# configured threshold crossed, it claims the alert with one conditional update of
# ``SiteAiBudgetDoc.alerted[<month>]`` (only if the stored value is lower), so two
# workers settling at once alert once. A claimed alert becomes a workspace-owner
# notification (kind ``site_ai_budget``) and a ``site.ai_budget`` bus event to the
# site owner. Everything here is best effort and never raises: an alert must not
# fail the call that triggered it.

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pymongo.errors import DuplicateKeyError

from pocketpaw_ee.cloud.models.site_ai import SiteAiBudgetDoc
from pocketpaw_ee.site_ai import service

logger = logging.getLogger(__name__)

NOTIFY_KIND = "site_ai_budget"
NOTIFY_SOURCE_TYPE = "site_ai_site"


def crossed(spent: int, cap: int, thresholds: list[int]) -> int:
    """The highest threshold (percent) ``spent`` has reached of ``cap``; 0 for none."""
    if cap <= 0:
        return 0
    hit = [t for t in thresholds if spent * 100 >= cap * t]
    return max(hit) if hit else 0


async def _claim(workspace_id: str, pocket_id: str, period: str, threshold: int) -> bool:
    budget = await service.load_budget(workspace_id, pocket_id)
    if budget.id is None:
        try:
            await budget.insert()
        except DuplicateKeyError:
            pass
    field = f"alerted.{period}"
    result = await SiteAiBudgetDoc.get_pymongo_collection().update_one(
        {
            "workspace": workspace_id,
            "pocket_id": pocket_id,
            "$or": [{field: {"$exists": False}}, {field: {"$lt": threshold}}],
        },
        {"$set": {field: threshold}},
    )
    return bool(result.modified_count)


async def check(site: Any, workspace_id: str, pocket_id: str) -> None:
    try:
        from pocketpaw_ee.cloud.billing import site_plans

        now = datetime.now(UTC)
        period = service.month_period(now)
        budget = await service.load_budget(workspace_id, pocket_id)
        cap = min(
            int(budget.monthly_cap_micro),
            site_plans.site_ai_allowance_micro(service.plan_key(site)),
        )
        spent = await service.spent_micro(workspace_id, pocket_id, "month", period)
        threshold = crossed(spent, cap, list(budget.alert_thresholds))
        if not threshold or int((budget.alerted or {}).get(period, 0)) >= threshold:
            return
        if not await _claim(workspace_id, pocket_id, period, threshold):
            return
        await _notify(site, workspace_id, pocket_id, period, threshold, spent, cap)
    except Exception:  # noqa: BLE001 - see the header
        logger.warning("site_ai: budget alert failed for %s", pocket_id, exc_info=True)


async def _notify(
    site: Any, workspace_id: str, pocket_id: str, period: str, threshold: int, spent: int, cap: int
) -> None:
    data = {
        "workspace_id": workspace_id,
        "pocket_id": pocket_id,
        "site_id": str(getattr(site, "id", "") or ""),
        "owner": str(getattr(site, "owner", "") or ""),
        "period": period,
        "threshold": threshold,
        "spent_month_micro": spent,
        "monthly_cap_micro": cap,
    }
    try:
        from pocketpaw_ee.cloud._core.realtime.emit import emit
        from pocketpaw_ee.cloud._core.realtime.events import SiteAiBudget

        await emit(SiteAiBudget(data=data))
    except Exception:  # noqa: BLE001
        logger.debug("site_ai: budget event not emitted", exc_info=True)
    try:
        from pocketpaw_ee.cloud.notifications import service as notifications_service
        from pocketpaw_ee.cloud.notifications.domain import NotificationSource
        from pocketpaw_ee.paw_bar.notify import resolve_workspace_owner, safe_preview

        recipient = await resolve_workspace_owner(workspace_id)
        if not recipient:
            return
        name = safe_preview(getattr(site, "name", "") or "", cap=80) or "Your site"
        if threshold >= 100:
            title = f"{name}: AI budget used up for this month"
            body = "AI on the site is resting until the month resets or you raise the cap."
        else:
            title = f"{name}: {threshold}% of this month's AI budget used"
            body = "Raise the monthly cap in the site's AI settings if you expect more traffic."
        await notifications_service.create(
            workspace_id=workspace_id,
            recipient=recipient,
            kind=NOTIFY_KIND,
            title=title,
            body=body,
            source=NotificationSource(
                type=NOTIFY_SOURCE_TYPE, id=f"{pocket_id}:{period}", pocket_id=pocket_id
            ),
        )
    except Exception:  # noqa: BLE001
        logger.debug("site_ai: budget notification failed", exc_info=True)


__all__ = ["NOTIFY_KIND", "check", "crossed"]
