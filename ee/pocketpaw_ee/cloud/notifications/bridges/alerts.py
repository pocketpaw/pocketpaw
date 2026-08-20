"""OSS operational alerts → workspace-bell notifications.

Created 2026-08-06 (feat/coupling-alerts-to-bell, T-10). The OSS
``pocketpaw.alert_manager`` publishes ``SystemEvent(event_type="alert")``
on the in-process pocketpaw MessageBus for budget_exhausted /
budget_warning / error_spike / tool_degradation / channel_disconnect.
Those events previously stopped at the OSS ring buffer + dashboard WS —
they never became Notification rows, so the workspace bell (and the
Slack/webhook external delivery that ``notifications_service.create``
fans into) stayed dark exactly when nobody was watching the dashboard.

This bridge subscribes to the OSS bus (the same seam
``sessions/title_listener.py`` uses) and fans each alert into
``notifications_service.create`` for the workspace admins. The
AlertManager's ring buffer is untouched — the bridge is purely additive,
and OSS-only deployments never load this module.

WORKSPACE RESOLUTION (design decision — read before changing)
-------------------------------------------------------------
The OSS AlertManager is single-tenant: its alerts are INSTANCE-scoped
(the process budget cap, the process error rate, the box's channel
adapters) and carry no workspace attribution at all. On a cloud
deployment we therefore notify the admins of the instance's FIRST-CREATED
workspace only — the workspace ``seed_default_workspace`` mints for the
platform operator at first boot. Rationale:

- On the common deployment shapes (dedicated box / PEE / small cloud)
  there is exactly one workspace, so "first workspace's admins" IS "the
  admins".
- On a multi-workspace box, instance internals (budget numbers, error
  rates, channel state) belong to the operator; fanning them to every
  tenant's admins would leak cross-tenant operational data. Conservative
  choice: operator workspace only.
- If alerts ever grow per-workspace attribution (e.g. per-tenant budget
  caps), route attributable alerts to their own workspace's admins and
  keep this default only for unattributable ones.

Resolution goes through ``workspace_service.get_default_workspace_id()``
(oldest workspace by ``_id``) + ``workspace_service.list_admin_ids()`` —
the same admin-audience helper the realtime AudienceResolver uses.

NOTIFICATION SHAPE / FE FOLLOW-UP
---------------------------------
``kind`` is ``f"alert_{alert_type}"`` (e.g. ``alert_budget_exhausted``).
``source`` is ``NotificationSource(type="alert", id=<alert_type>)`` —
alerts have no entity page. The PUSH side is handled:
``push/listeners._target_url`` has an explicit ``alert`` arm returning
None, so an alert push carries no deep link rather than a dead
``/chat/<alert_type>`` one. Remaining FE follow-up (lands separately):
paw-enterprise's ``core/notifications/target.ts`` still routes unknown
source types to ``/chat/{id}`` in the bell — add a ``case 'alert'`` to
``targetUrl()`` mapping to the ``/activity`` surface (the
operational-events page), the sensible landing spot until a dedicated
alerts surface exists.
"""

from __future__ import annotations

import logging
from typing import Any

from pocketpaw.bus.events import SystemEvent

logger = logging.getLogger(__name__)

# Friendly titles per known alert_type; unknown types fall back to a
# de-underscored capitalisation so a new OSS alert type still reads OK.
_TITLES: dict[str, str] = {
    "budget_exhausted": "Budget exhausted",
    "budget_warning": "Budget warning",
    "error_spike": "Error spike detected",
    "tool_degradation": "Tool degraded",
    "channel_disconnect": "Channel disconnected",
}


async def _on_system_event(event: SystemEvent) -> None:
    """OSS-bus subscriber. MUST never raise — the AlertManager's publish
    path gathers subscriber exceptions, but a raise here would still log
    as a bus-level error and hide real fan-out bugs. All failure modes
    log-and-return instead."""
    if event.event_type != "alert":
        return
    try:
        await _fan_out(event.data)
    except Exception:
        logger.exception("alert → notification fan-out failed")


async def _fan_out(raw: Any) -> None:
    data: dict[str, Any] = raw if isinstance(raw, dict) else {}
    alert_type = data.get("alert_type")
    if not isinstance(alert_type, str) or not alert_type:
        logger.info("alert event missing alert_type; skipping: %r", data)
        return

    message = str(data.get("message") or "")

    # Lazy imports: tolerate contexts where the cloud DB / Beanie is not
    # initialized (OSS process that somehow loaded this module, unit
    # tests without the mongo fixture) — same posture as the meetings
    # bridge and the title listener.
    try:
        from pocketpaw_ee.cloud.workspace import service as workspace_service

        workspace_id = await workspace_service.get_default_workspace_id()
        if not workspace_id:
            logger.info("no workspace exists yet; dropping alert %s", alert_type)
            return
        admins = await workspace_service.list_admin_ids(workspace_id)
    except Exception:
        logger.exception("could not resolve alert audience for %s", alert_type)
        return

    if not admins:
        logger.info(
            "default workspace %s has no admins; dropping alert %s", workspace_id, alert_type
        )
        return

    kind = f"alert_{alert_type}"
    title = _TITLES.get(alert_type, alert_type.replace("_", " ").capitalize())
    body = message or f"Operational alert: {alert_type}"

    from pocketpaw_ee.cloud.notifications.domain import NotificationSource

    for recipient in admins:
        # Per-recipient isolation: one failing insert must not starve the
        # remaining admins of the alert.
        try:
            from pocketpaw_ee.cloud.notifications import service as notifications_service

            await notifications_service.create(
                workspace_id=workspace_id,
                recipient=recipient,
                kind=kind,
                title=title,
                body=body,
                source=NotificationSource(type="alert", id=alert_type),
            )
        except Exception:
            logger.exception("failed to create %s notification for admin=%s", kind, recipient)


def register_alert_notification_listeners() -> None:
    """Idempotently subscribe the alert bridge to the OSS MessageBus.

    Unsubscribe-first (a no-op when absent) instead of a module-level
    flag: the bus singleton is reset between tests via
    ``pocketpaw.lifecycle``, and a sticky flag would silently skip
    re-registration against the fresh bus.
    """
    from pocketpaw.bus import get_message_bus

    bus = get_message_bus()
    bus.unsubscribe_system(_on_system_event)
    bus.subscribe_system(_on_system_event)
    logger.info("registered OSS alert → notifications bridge")


__all__ = ["register_alert_notification_listeners"]
