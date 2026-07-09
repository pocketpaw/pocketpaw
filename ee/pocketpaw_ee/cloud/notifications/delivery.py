# ee/pocketpaw_ee/cloud/notifications/delivery.py
# Created: 2026-07-08 (feat/external-alerting-delivery) — external fan-out for
# cloud notifications (Criterion 1: get alerts OUT of the app). Every cloud
# notification funnels through ``notifications.service.create`` (tasks, meetings,
# mentions all call it); this module POSTs that notification to the workspace's
# configured Slack incoming-webhook and/or generic HTTPS webhook.
#
# Contract with the caller: ``_deliver_external`` is FIRE-AND-FORGET / NEVER-RAISE
# (modeled on ``_core/realtime/emit.py`` and ``audit/webhooks.deliver``). It is
# awaited inline right after the ``emit(NotificationNew(...))`` in ``create``, so
# a dead/slow/malicious webhook must NOT be able to roll back the notification
# insert or bubble an exception out of ``create``. Every network call is wrapped
# and every URL is bounded by a short timeout so a hung endpoint can't stall the
# insert response. A per-workspace kill switch (``enabled``) and per-kind routing
# live on ``NotificationDeliveryConfig``.
#
# SSRF: the webhook URLs are workspace-admin-supplied, so a POST from the server
# to an arbitrary URL is an SSRF vector. ``is_safe_webhook_url`` requires https://
# and rejects literal private/loopback IPs and known-internal hostnames. It runs
# at write time (the PUT route rejects a bad URL up front) AND here at delivery
# time (defense-in-depth against a stored value that later became unsafe). Full
# DNS-resolution hardening (like ``audit.webhooks._validate_url_safety``) is a
# follow-up; this is the baseline.

from __future__ import annotations

import ipaddress
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.notifications.domain import Notification

logger = logging.getLogger(__name__)

# Bounded so a hung endpoint can't stall the notification insert response.
_DELIVERY_TIMEOUT_SECONDS = 5.0

# Sink names. Kept as constants so ``routes`` values and future sinks stay
# consistent. A third sink ("email") layers on here + a new URL field.
SINK_SLACK = "slack"
SINK_WEBHOOK = "webhook"

# Hostnames that point at internal infrastructure on common cloud platforms /
# dev boxes. Rejected even before any IP check (mirrors audit.webhooks).
_FORBIDDEN_HOSTNAMES = frozenset(
    {
        "localhost",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)


def _ip_is_unsafe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_safe_webhook_url(url: str | None) -> bool:
    """True when ``url`` is a plausible-safe external https webhook target.

    Baseline SSRF guard for workspace-admin-supplied URLs: requires https://,
    a hostname, a non-forbidden hostname, and — when the host is a literal IP —
    a public address. A hostname that resolves to a private IP is NOT caught
    here (no DNS lookup in the hot path); that DNS-resolution hardening is a
    follow-up. ``None`` / empty is "no sink", which is safe (returns False).
    """
    if not url:
        return False
    if not url.startswith("https://"):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname in _FORBIDDEN_HOSTNAMES:
        return False
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and _ip_is_unsafe(literal_ip):
        return False
    return True


def _slack_payload(notification: Notification) -> dict:
    """Slack incoming-webhook shape. Slack renders ``text`` as the message body;
    we lead with the title and append the body when present."""
    text = notification.title
    if notification.body:
        text = f"{text}\n{notification.body}"
    return {"text": text}


def _generic_payload(notification: Notification) -> dict:
    """Full notification payload for a generic consumer. Field names mirror the
    domain object so a receiver can key off ``kind`` / ``workspace_id``."""
    return {
        "id": notification.id,
        "workspace_id": notification.workspace_id,
        "recipient_id": notification.recipient_id,
        "actor_id": notification.actor_id,
        "kind": notification.kind,
        "title": notification.title,
        "body": notification.body,
    }


async def _load_config(workspace_id: str):
    """Load the workspace's delivery config, or ``None`` when unset.

    Best-effort: a missing doc (the common case) or a read failure returns
    ``None`` so a Mongo hiccup can never take down ``create``. Imported inside
    the function to keep the module-import graph light (same lazy-import style
    the belt service uses for its config doc)."""
    from pocketpaw_ee.cloud.models.notification_delivery import NotificationDeliveryConfig

    return await NotificationDeliveryConfig.find_one(
        NotificationDeliveryConfig.workspace == workspace_id
    )


def _resolve_sinks(config, kind: str) -> list[tuple[str, str]]:
    """Return the ``(sink_name, url)`` pairs to deliver ``kind`` to.

    A sink is eligible when its URL is set AND passes the safety check. Routing:
    if ``config.routes`` has an entry for ``kind``, only the named sinks are
    used; otherwise every configured+safe sink is used (deliver-all default).
    """
    available: dict[str, str] = {}
    if is_safe_webhook_url(config.slack_webhook_url):
        available[SINK_SLACK] = config.slack_webhook_url
    if is_safe_webhook_url(config.webhook_url):
        available[SINK_WEBHOOK] = config.webhook_url

    allowed = config.routes.get(kind) if config.routes else None
    if allowed is not None:
        return [(name, url) for name, url in available.items() if name in allowed]
    return list(available.items())


async def _post_one(
    client: httpx.AsyncClient,
    sink_name: str,
    url: str,
    notification: Notification,
) -> None:
    """POST the notification to one sink. Raises on failure — the caller
    swallows it per-sink so one dead sink never blocks the others."""
    payload = _slack_payload(notification) if sink_name == SINK_SLACK else _generic_payload(
        notification
    )
    resp = await client.post(url, json=payload, timeout=_DELIVERY_TIMEOUT_SECONDS)
    # 2xx is success; anything else is logged but not retried in v1.
    if not (200 <= resp.status_code < 300):
        logger.warning(
            "notification external delivery: %s returned http %s", sink_name, resp.status_code
        )


async def _deliver_external(notification: Notification) -> None:
    """Fan a freshly-created notification out to the workspace's external sinks.

    NEVER raises — every failure mode (no config, disabled, unsafe URL, dead
    endpoint, Mongo hiccup) is swallowed so the notification insert + realtime
    emit that preceded this call can never be rolled back by a delivery problem.
    """
    try:
        config = await _load_config(notification.workspace_id)
        if config is None or not config.enabled:
            return
        sinks = _resolve_sinks(config, notification.kind)
        if not sinks:
            return
        async with httpx.AsyncClient() as client:
            for sink_name, url in sinks:
                try:
                    await _post_one(client, sink_name, url, notification)
                except Exception:
                    logger.warning(
                        "notification external delivery to %s failed", sink_name, exc_info=True
                    )
    except Exception:
        logger.warning("notification external delivery fan-out crashed", exc_info=True)


__all__ = ["_deliver_external", "is_safe_webhook_url"]
