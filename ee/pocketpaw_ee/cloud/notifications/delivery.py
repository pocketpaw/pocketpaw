# ee/pocketpaw_ee/cloud/notifications/delivery.py
# Workspace-level external fan-out for cloud notifications: decides which of the
# workspace's sinks (Slack incoming-webhook, signed generic webhook) a
# notification goes to, and ENQUEUES one ``notification_outbox`` row per sink.
# Nothing here does HTTP; ``notifications.outbox`` sends, retries and gives up.
#
# Contract: NEVER-RAISE. ``enqueue_external`` is awaited by ``service.create``
# after the realtime emit and only writes outbox rows, so the request path never
# waits on a remote endpoint and a Mongo hiccup can't roll back the insert.
# ``schedule_external_many`` does the same for a same-workspace batch in a
# bounded background task, reading the config once. ``enqueue_workspace_event``
# is the lead/handoff path: one delivery per EVENT (not per recipient), with a
# typed webhook event whose data the outbox loads at send time.
#
# Routing lives on ``NotificationDeliveryConfig``: ``enabled`` master switch,
# per-kind ``routes`` narrowing, and a webhook that the outbox switches off
# (``webhook_disabled_at``) after 10 consecutive dead deliveries.
#
# SSRF: URLs are admin-supplied. ``is_safe_webhook_url`` is the sync baseline
# (https only, forbidden hostnames, literal IPs normalized the way the OS
# resolver would so decimal/hex/octal/short-dotted encodings are caught).
# ``validate_webhook_url`` adds the DNS-resolving check from
# ``audit.webhooks.validate_url_safety`` (every resolved address must be public).
# Both run on save and again before every send (DNS rebinding).

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pocketpaw_ee.cloud._core.errors import Forbidden

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.notifications.domain import Notification

logger = logging.getLogger(__name__)

# Batch enqueues (``schedule_external_many``) run as background tasks. At most
# this many run at once; strong refs live in ``_inflight`` so a task isn't
# garbage-collected mid-flight.
_MAX_CONCURRENT_BATCHES = 8
_batch_slots: asyncio.Semaphore | None = None
_batch_slots_loop: asyncio.AbstractEventLoop | None = None
_inflight: set[asyncio.Task[None]] = set()

SINK_SLACK = "slack"
SINK_WEBHOOK = "webhook"
SINK_EMAIL = "email"

# The webhook ``type`` for a plain notification (not a lead/booking event).
EVENT_NOTIFICATION = "notification.created"

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
    # ``is_global`` is False for private, loopback, link-local, reserved,
    # unspecified AND shared/CGNAT space (100.64.0.0/10, which holds cloud
    # metadata endpoints such as 100.100.100.200); multicast is "global" to
    # ipaddress but never a webhook receiver.
    return not ip.is_global or ip.is_multicast


def _host_as_literal_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Interpret ``hostname`` as an IP the way the OS resolver / httpx would, or None.

    ``ipaddress.ip_address`` only takes the strict form, so it misses the
    encodings an SSRF payload uses: a decimal integer (``2852039166`` is
    169.254.169.254), hex/octal integers (``0x7f000001``), short-dotted
    (``127.1``). In order: the strict literal (the only IPv6 path), a bare
    integer via ``int(host, 0)``, then ``socket.inet_aton`` (the liberal parser
    ``getaddrinfo`` shares, which also catches leading-zero octal). Returns None
    for a real DNS name.
    """
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(int(hostname, 0))
    except (ValueError, OverflowError):
        pass
    try:
        packed = socket.inet_aton(hostname)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed)


def is_safe_webhook_url(url: str | None) -> bool:
    """Sync SSRF baseline: https, a non-forbidden host, no private literal IP in
    any encoding. ``None`` / empty means "no sink" and returns False. DNS is
    checked separately by ``validate_webhook_url``."""
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
    # Any port 1-65535, the same rule the send path (SafeFetcher.post) applies.
    try:
        port = parsed.port
    except ValueError:
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    literal_ip = _host_as_literal_ip(hostname)
    if literal_ip is not None and _ip_is_unsafe(literal_ip):
        return False
    return True


async def validate_webhook_url(url: str) -> None:
    """Raise ``Forbidden`` unless ``url`` is safe, including where its host
    RESOLVES to. Reuses the audit webhooks' DNS check rather than a copy."""
    from pocketpaw_ee.cloud.audit.webhooks import validate_url_safety

    if not is_safe_webhook_url(url):
        raise Forbidden(
            "notifications.invalid_webhook_url",
            "Webhook URL must be an https:// URL to a public host.",
        )
    await validate_url_safety(url)


def _slack_payload(notification: Notification) -> dict:
    """Slack incoming-webhook shape: ``text`` = title, then body when present."""
    text = notification.title
    if notification.body:
        text = f"{text}\n{notification.body}"
    return {"text": text}


def _generic_data(notification: Notification) -> dict:
    """``data`` of a ``notification.created`` webhook event, also sent flat at
    the top level of the body for consumers of the pre-envelope shape."""
    return {
        "id": notification.id,
        "workspace_id": notification.workspace_id,
        "recipient_id": notification.recipient_id,
        "actor_id": notification.actor_id,
        "kind": notification.kind,
        "title": notification.title,
        "body": notification.body,
    }


def new_event_envelope(event_type: str, **fields: Any) -> dict[str, Any]:
    """The stored half of a webhook event: a stable id + type + created_at.
    ``fields`` is either ``data`` (sent as-is) or ``lead_id`` (the outbox loads
    the lead at send time)."""
    return {
        "event_id": f"evt_{uuid.uuid4().hex}",
        "event_type": event_type,
        "created_at": datetime.now(UTC).isoformat(),
        **fields,
    }


async def _load_config(workspace_id: str):
    """The workspace's delivery config, or None."""
    from pocketpaw_ee.cloud.models.notification_delivery import NotificationDeliveryConfig

    return await NotificationDeliveryConfig.find_one(
        NotificationDeliveryConfig.workspace == workspace_id
    )


def _resolve_sinks(config, kind: str) -> list[tuple[str, str]]:
    """``(sink_name, url)`` pairs for ``kind``. A sink is eligible when its URL
    passes the sync safety check (and, for the webhook, it isn't switched off);
    a ``routes`` entry for ``kind`` narrows to the named sinks."""
    available: dict[str, str] = {}
    if is_safe_webhook_url(config.slack_webhook_url):
        available[SINK_SLACK] = config.slack_webhook_url
    webhook_off = getattr(config, "webhook_disabled_at", None) is not None
    if is_safe_webhook_url(config.webhook_url) and not webhook_off:
        available[SINK_WEBHOOK] = config.webhook_url

    allowed = config.routes.get(kind) if config.routes else None
    if allowed is not None:
        return [(name, url) for name, url in available.items() if name in allowed]
    return list(available.items())


def _rows_for(config, notification: Notification) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sink, url in _resolve_sinks(config, notification.kind):
        if sink == SINK_SLACK:
            payload = _slack_payload(notification)
            ref = ""
        else:
            data = _generic_data(notification)
            # Back-compat: the workspace webhook used to receive the flat
            # notification fields at the top level. They ride along (deprecated)
            # beside the envelope. ``id`` is the notification id in both shapes,
            # so the event id IS the notification id here (one delivery per
            # notification per sink, so it is still a stable dedupe key).
            payload = new_event_envelope(EVENT_NOTIFICATION, data=data, legacy=data)
            payload["event_id"] = notification.id
            ref = f"workspace:{notification.workspace_id}"
        rows.append(
            {
                "workspace": notification.workspace_id,
                "kind": notification.kind,
                "sink": sink,
                "target": url,
                "payload": payload,
                "webhook_ref": ref,
            }
        )
    return rows


async def enqueue_external(notification: Notification) -> None:
    """Queue a fresh notification for the workspace's external sinks. Never raises."""
    from pocketpaw_ee.cloud.notifications import outbox

    try:
        config = await _load_config(notification.workspace_id)
        if config is None or not config.enabled:
            return
        rows = _rows_for(config, notification)
        if rows:
            await outbox.enqueue_many(rows)
    except Exception:
        logger.warning("notification external enqueue crashed", exc_info=True)


async def enqueue_workspace_event(
    *,
    workspace_id: str,
    kind: str,
    slack_text: str,
    webhook_event: dict[str, Any],
    include_webhook: bool = True,
) -> None:
    """Queue ONE delivery of a site event (lead, handoff) to the workspace's
    Slack and webhook sinks, honouring ``enabled`` and ``routes``. Pass
    ``include_webhook=False`` when a site-level webhook already carries it.
    Never raises."""
    from pocketpaw_ee.cloud.notifications import outbox

    try:
        config = await _load_config(workspace_id)
        if config is None or not config.enabled:
            return
        rows: list[dict[str, Any]] = []
        for sink, url in _resolve_sinks(config, kind):
            row: dict[str, Any] = {
                "workspace": workspace_id,
                "kind": kind,
                "sink": sink,
                "target": url,
            }
            if sink == SINK_SLACK:
                row["payload"] = {"text": slack_text}
            elif include_webhook:
                row["payload"] = dict(webhook_event)
                row["webhook_ref"] = f"workspace:{workspace_id}"
            else:
                continue
            rows.append(row)
        if rows:
            await outbox.enqueue_many(rows)
    except Exception:
        logger.warning("workspace event enqueue crashed", exc_info=True)


def _get_batch_slots() -> asyncio.Semaphore:
    """The module semaphore, rebuilt when the running loop changes."""
    global _batch_slots, _batch_slots_loop
    loop = asyncio.get_running_loop()
    if _batch_slots is None or _batch_slots_loop is not loop:
        _batch_slots = asyncio.Semaphore(_MAX_CONCURRENT_BATCHES)
        _batch_slots_loop = loop
    return _batch_slots


async def _enqueue_external_many(notifications: list[Notification]) -> None:
    """Enqueue a same-workspace batch with ONE config read. Never raises."""
    from pocketpaw_ee.cloud.notifications import outbox

    async with _get_batch_slots():
        try:
            config = await _load_config(notifications[0].workspace_id)
            if config is None or not config.enabled:
                return
            rows = [row for n in notifications for row in _rows_for(config, n)]
            if rows:
                await outbox.enqueue_many(rows)
        except Exception:
            logger.warning("notification batch external enqueue crashed", exc_info=True)


def schedule_external_many(notifications: list[Notification]) -> None:
    """Fire-and-forget enqueue for a same-workspace batch."""
    if not notifications:
        return
    try:
        task = asyncio.create_task(_enqueue_external_many(notifications))
    except RuntimeError:
        logger.debug("notification batch delivery: no running loop")
        return
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


__all__ = [
    "EVENT_NOTIFICATION",
    "SINK_EMAIL",
    "SINK_SLACK",
    "SINK_WEBHOOK",
    "enqueue_external",
    "enqueue_workspace_event",
    "is_safe_webhook_url",
    "new_event_envelope",
    "schedule_external_many",
    "validate_webhook_url",
]
