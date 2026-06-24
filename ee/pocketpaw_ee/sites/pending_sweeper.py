# ee/pocketpaw_ee/sites/pending_sweeper.py — pending-site reconciliation sweeper
# (feat/billing-lifecycle, review loose end C — lost-webhook visibility).
#
# Created 2026-06-24 (feat/billing-lifecycle): a charge-first PAID site is created
# as PENDING (deployed=False, subscription_status="pending") and is deployed live
# only when the per-site ``subscription.active`` webhook confirms payment
# (``sites.service.activate_site``). If that webhook is LOST or badly DELAYED, the
# site sits pending forever with no operator signal. This sweeper periodically
# finds PAID sites stuck in ``subscription_status == "pending"`` (not deployed)
# older than a configurable threshold and LOGS them at WARNING so an operator can
# investigate a missing/delayed webhook.
#
# VISIBILITY ONLY — it does NOT auto-deploy and does NOT auto-cancel. Auto-deploy
# would take a site live without confirmed payment; auto-cancel needs a grace
# policy that is a deliberate follow-up (see TODO(billing-lapse) in
# ee/pocketpaw_ee/cloud/billing/service.py). The sweeper just surfaces the stuck
# set and returns it.
#
# Wired into the same boot + 5-minute heartbeat as the chat-runs sweeper
# (``ee.pocketpaw_ee.extensions._sweeper_loop`` / ``start_run_sweeper``), each
# sweep in its own try so one failing can't suppress the other.

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

logger = logging.getLogger(__name__)

# Default hours a paid site may sit pending before the sweeper flags it. Tuned to
# comfortably exceed Dodo's webhook retry window so a transient delivery delay
# does not produce noise — a site still pending after this long signals a likely
# LOST webhook, not an in-flight one. Overridable via
# POCKETPAW_SITE_PENDING_ALERT_HOURS.
_DEFAULT_PENDING_ALERT_HOURS = 24
# Cap per tick so a large backlog can't wedge the shared heartbeat.
_SWEEP_BATCH_LIMIT = 200


def _pending_alert_hours() -> float:
    """Hours a paid site may sit pending before the sweeper flags it.

    Reads ``POCKETPAW_SITE_PENDING_ALERT_HOURS`` (a positive number) and falls
    back to the default for an unset / malformed / non-positive value. Read at
    sweep time (not import) so a deployment can tune it without a restart, and so
    a test can monkeypatch the env var per case. Reads the env var directly rather
    than ``get_settings()`` so the sweep stays importable + runnable in a context
    that hasn't loaded the full settings object.
    """
    raw = os.environ.get("POCKETPAW_SITE_PENDING_ALERT_HOURS")
    try:
        hours = float(raw) if raw else float(_DEFAULT_PENDING_ALERT_HOURS)
    except (TypeError, ValueError):
        return float(_DEFAULT_PENDING_ALERT_HOURS)
    return hours if hours > 0 else float(_DEFAULT_PENDING_ALERT_HOURS)


async def sweep_pending_sites(*, older_than_hours: float | None = None) -> list[_SiteDoc]:
    """Find + log PAID sites stuck in ``subscription_status == "pending"`` (not
    deployed) older than the threshold. Returns the flagged docs.

    VISIBILITY ONLY — the sweeper NEVER mutates a site (no auto-deploy, no
    auto-cancel). It surfaces sites whose charge-first activation never landed (a
    lost / delayed ``subscription.active`` webhook) so an operator can investigate.

    ``older_than_hours`` overrides the configured threshold (default
    ``_pending_alert_hours()``). A site is "stuck" when it is pending AND not
    deployed AND its ``createdAt`` is older than the cutoff. A recently-pending
    site (within the window) and an active/deployed site are never flagged.
    """
    hours = older_than_hours if older_than_hours is not None else _pending_alert_hours()
    cutoff = datetime.now(UTC) - timedelta(hours=hours)

    stuck = (
        await _SiteDoc.find(
            {"subscription_status": "pending", "deployed": False},
            _SiteDoc.createdAt < cutoff,
        )
        .limit(_SWEEP_BATCH_LIMIT)
        .to_list()
    )
    if not stuck:
        return []

    for doc in stuck:
        logger.warning(
            "sites.pending_sweeper: site %s (workspace=%s pocket=%s tier=%s) has been "
            "pending payment since %s (> %.1fh) — a subscription.active webhook may be "
            "lost or delayed; the site is NOT auto-deployed or auto-cancelled, operator "
            "review required",
            str(doc.id),
            doc.workspace,
            doc.pocket_id,
            doc.plan_tier or "",
            doc.createdAt.isoformat() if getattr(doc, "createdAt", None) else "?",
            hours,
        )
    logger.warning(
        "sites.pending_sweeper: %d site(s) stuck pending past %.1fh",
        len(stuck),
        hours,
    )
    return stuck
