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
#
# Updated 2026-07-09 (fix/ssrf-encoding-bypass): close the alternate-ENCODING
# bypass. The old guard only ran ``ipaddress.ip_address(hostname)`` and treated a
# parse FAILURE as "not an IP" — but that raises on encoded forms (decimal
# ``2852039166`` == 169.254.169.254 metadata, ``0x7f000001`` / ``017700000001`` /
# ``127.1`` == 127.0.0.1), so the guard returned True while ``getaddrinfo`` / httpx
# still resolved them to metadata / loopback. ``_host_as_literal_ip`` now normalizes
# the host (strict literal -> ``int(host, 0)`` -> ``socket.inet_aton``) before the
# unsafe-IP check. DNS-name-resolution hardening (a host that RESOLVES to a private
# IP) remains the documented follow-up — this needs no DNS control to exploit.

from __future__ import annotations

import ipaddress
import logging
import socket
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


def _host_as_literal_ip(hostname: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Interpret ``hostname`` as an IP the way the OS resolver / httpx would, or None.

    ``ipaddress.ip_address`` only accepts the strict dotted-quad (and IPv6) form,
    so it MISSES the alternate encodings an SSRF payload uses — a decimal integer
    (``2852039166`` == 169.254.169.254 cloud metadata), a hex/octal integer
    (``0x7f000001`` / ``017700000001`` == 127.0.0.1), or a short-dotted form
    (``127.1``). ``getaddrinfo`` / httpx DO resolve those to the loopback / metadata
    address, so the guard must normalize them before deciding. We try, in order:

      1. the strict literal (also the ONLY IPv6 path);
      2. a bare integer via ``int(host, 0)`` (decimal / ``0x`` hex / ``0o`` octal);
      3. ``socket.inet_aton`` — the liberal C parser ``getaddrinfo`` shares, which
         covers ``a`` / ``a.b`` / ``a.b.c`` / ``a.b.c.d`` with decimal, leading-zero
         octal, or ``0x`` hex parts (this is what catches ``127.1`` and the bare
         leading-zero octal ``017700000001`` that ``int(host, 0)`` rejects).

    Returns None for a genuine DNS hostname (``hooks.slack.com``) so it stays allowed;
    hostnames that RESOLVE to a private IP are still not caught here (no DNS lookup in
    the hot path) — that is the documented follow-up. This only closes the alternate-
    ENCODING bypass, which needs no DNS control.
    """
    # 1. Strict literal (dotted-quad IPv4 or any IPv6).
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        pass
    # 2. Bare integer form: decimal / 0x-hex / 0o-octal. ``int(host, 0)`` rejects a
    #    bare leading-zero octal (e.g. "017700000001"); step 3 covers that.
    try:
        return ipaddress.IPv4Address(int(hostname, 0))
    except (ValueError, OverflowError):
        pass
    # 3. inet_aton — liberal short-dotted / leading-zero-octal / hex parsing.
    try:
        packed = socket.inet_aton(hostname)
    except OSError:
        return None
    return ipaddress.IPv4Address(packed)


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
    # Normalize the host to the IP the OS resolver would use BEFORE deciding — this
    # catches the alternate-encoding SSRF bypasses (decimal/hex/octal integer,
    # short-dotted) that a naive ``ipaddress.ip_address`` parse silently lets
    # through while ``getaddrinfo`` / httpx resolve them to metadata / loopback.
    literal_ip = _host_as_literal_ip(hostname)
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
