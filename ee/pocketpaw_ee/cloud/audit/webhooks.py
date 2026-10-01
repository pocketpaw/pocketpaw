# 2026-10-01 (CN-2): deliveries go out through the pinned
#   ``sites.safe_fetch.SafeFetcher.post`` instead of a plain httpx client, so the
#   socket connects to the IP the SSRF check validated (closes the DNS-rebind
#   window between check and connect). Headers, failure counting and
#   auto-disable are unchanged; a target the pinned fetcher refuses disables the
#   webhook like the pre-check does.
"""SIEM webhook delivery for workspace audit events (Wave 3 Task 15).

External HTTPS endpoint registry. Each enabled webhook receives a signed
POST per audit event. Signature scheme:

    body = f"{timestamp}.{json_payload}"
    sig  = HMAC-SHA256(secret, body)

Headers:
    X-Paw-Audit-Timestamp: <unix-seconds>
    X-Paw-Audit-Signature: sha256=<hex>

Receiver guidance — what your SIEM endpoint must do to be safe:

  1. Re-compute the HMAC with the shared secret over
     ``f"{header_timestamp}.{raw_request_body}"`` and ``hmac.compare_digest``
     it against the signature header. Treat any mismatch as a hard reject.
  2. Verify the timestamp is fresh (recommended ≤ 5 minutes from "now").
     Without this, an attacker who once captured a signed delivery can
     replay it forever — the signature alone is timeless.
  3. Treat the body as untrusted JSON; never echo it back into HTML
     or shell contexts.

Auto-disable after 10 consecutive failures. Secrets are encrypted at
rest with the shared SSO Fernet key; URLs are revalidated per delivery, and
the POST goes through ``sites.safe_fetch.SafeFetcher`` which resolves once,
checks every address, and pins the connection to the validated IP (Host header
and TLS SNI keep the hostname), so DNS rebinding between check and connect
cannot reach a private address. The SSRF check resolves hostnames through
the loop's async resolver (never a sync ``getaddrinfo`` on the event loop), and
fire-and-forget deliveries run at most ``_MAX_CONCURRENT_DELIVERIES`` at once.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.auth.sso import crypto as _crypto
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.models.audit_webhook import AuditWebhook

logger = logging.getLogger(__name__)

_FAILURE_DISABLE_THRESHOLD = 10
_DELIVERY_TIMEOUT_SECONDS = 5.0
# A receiver's reply is read only for its status; anything past this is cut off.
_RESPONSE_CAP_BYTES = 64 * 1024
_USER_AGENT = "PocketPaw-Audit-Webhooks/1.0 (+https://pocketpaw.dev)"

# Why: asyncio.create_task only keeps a weakref; if the event loop GCs the
# task before it runs we silently lose deliveries (and Python logs a
# RuntimeWarning). Holding strong refs in a module-level set keeps them
# alive until done_callback discards.
_inflight_deliveries: set[asyncio.Task[None]] = set()

# Cap on concurrent ``deliver`` fan-outs. Every audit event schedules one; an
# audit burst (bulk invite, mass delete) would otherwise open one httpx client
# and one Mongo query per event all at once. Excess events wait their turn.
_MAX_CONCURRENT_DELIVERIES = 16
_delivery_slots: asyncio.Semaphore | None = None
_delivery_slots_loop: asyncio.AbstractEventLoop | None = None


def _get_delivery_slots() -> asyncio.Semaphore:
    """The module semaphore, rebuilt if the running loop changed (a semaphore
    that ever had a waiter is bound to that loop, and tests run many loops)."""
    global _delivery_slots, _delivery_slots_loop
    loop = asyncio.get_running_loop()
    if _delivery_slots is None or _delivery_slots_loop is not loop:
        _delivery_slots = asyncio.Semaphore(_MAX_CONCURRENT_DELIVERIES)
        _delivery_slots_loop = loop
    return _delivery_slots


def mint_secret() -> str:
    return secrets.token_urlsafe(32)


def _decrypt_secret(stored: str) -> str:
    """Return the raw signing secret from the persisted column.

    Why try/except: rows written before this change held the secret in
    plaintext. Fernet ciphertext starts with ``gAAAAA`` (base64 ``\\x80\\x00\\x00...``);
    legacy plaintext doesn't decode. On InvalidToken we treat the value
    as a legacy plaintext secret so old webhooks keep delivering, and
    log once so operators get nudged toward rotating.
    """
    try:
        return _crypto.decrypt(stored)
    except Exception:
        logger.warning("audit.webhook: secret appears unencrypted; rotate to re-encrypt")
        return stored


# Names that point at internal infrastructure on common cloud platforms
# and on dev boxes. Reject these at create time even before DNS resolution.
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


async def _resolve_addresses(hostname: str) -> list[str] | None:
    """Return all resolved IP strings, or None if DNS resolution failed.

    Why None on failure: an unresolvable hostname will fail at HTTP time
    anyway; raising here would also break dev/test setups that use made-up
    domains like ``siem.example.com``.
    """
    try:
        # The loop's resolver runs getaddrinfo in the executor; the sync call
        # would block every request in the process for the whole DNS lookup.
        infos = await asyncio.get_running_loop().getaddrinfo(hostname, None)
    except socket.gaierror:
        return None
    return [info[4][0] for info in infos]


async def _validate_url_safety(url: str) -> None:
    """Reject non-https + URLs whose hostname targets internal/private space.

    Defense against SSRF: a workspace admin should not be able to point
    a webhook at ``https://169.254.169.254/...`` and have us POST signed
    audit events into the cloud metadata service. Runs at create time
    AND per-delivery (the second call catches DNS rebinding).
    """
    if not url.startswith("https://"):
        raise Forbidden("webhooks.https_required", "Webhook URL must be https://")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise Forbidden("webhooks.invalid_url", "Webhook URL is malformed") from exc
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise Forbidden("webhooks.invalid_url", "Webhook URL missing hostname")
    if hostname in _FORBIDDEN_HOSTNAMES:
        raise Forbidden(
            "webhooks.private_address",
            f"Webhook hostname '{hostname}' is not allowed",
        )
    # Literal IP — check directly without DNS.
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if _ip_is_unsafe(literal_ip):
            raise Forbidden(
                "webhooks.private_address",
                "Webhook URL points at a private/loopback address",
            )
        return
    # Hostname — resolve and require every returned IP to be public.
    addresses = await _resolve_addresses(hostname)
    if addresses is None:
        return  # DNS failure; HTTP layer will surface the real error.
    for addr in addresses:
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_unsafe(ip):
            raise Forbidden(
                "webhooks.private_address",
                f"Webhook hostname '{hostname}' resolves to a non-public address",
            )


# Back-compat alias for any external caller that imported the private helper.
_require_https = _validate_url_safety

# Public name for the DNS-resolving SSRF check. ``notifications.delivery`` reuses
# it for the notification webhooks rather than keeping a second copy.
validate_url_safety = _validate_url_safety


def _resolve_id(webhook_id: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(webhook_id)
    except Exception as exc:
        raise NotFound("audit_webhook", webhook_id) from exc


async def create_webhook(
    workspace_id: str,
    url: str,
    created_by: str,
) -> tuple[AuditWebhook, str]:
    await _validate_url_safety(url)
    secret = mint_secret()
    doc = AuditWebhook(
        workspace=workspace_id,
        url=url,
        secret=_crypto.encrypt(secret),
        created_by=created_by,
    )
    await doc.insert()
    return doc, secret


async def list_webhooks(workspace_id: str) -> list[AuditWebhook]:
    return await AuditWebhook.find({"workspace": workspace_id}).to_list()


async def _get(workspace_id: str, webhook_id: str) -> AuditWebhook:
    oid = _resolve_id(webhook_id)
    doc = await AuditWebhook.find_one({"_id": oid, "workspace": workspace_id})
    if not doc:
        raise NotFound("audit_webhook", webhook_id)
    return doc


async def update_webhook(
    workspace_id: str,
    webhook_id: str,
    *,
    enabled: bool | None = None,
) -> AuditWebhook:
    doc = await _get(workspace_id, webhook_id)
    if enabled is not None:
        doc.enabled = enabled
    await doc.save()
    return doc


async def delete_webhook(workspace_id: str, webhook_id: str) -> None:
    doc = await _get(workspace_id, webhook_id)
    await doc.delete()


async def rotate_secret(workspace_id: str, webhook_id: str) -> tuple[AuditWebhook, str]:
    doc = await _get(workspace_id, webhook_id)
    new_secret = mint_secret()
    doc.secret = _crypto.encrypt(new_secret)
    await doc.save()
    return doc, new_secret


def _event_payload(event: AuditEvent) -> dict[str, Any]:
    return {
        "event_id": str(event.id),
        "workspace": event.workspace,
        "actor_id": event.actor_id,
        "action": event.action,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "metadata": dict(event.metadata or {}),
        "at": event.at.isoformat(),
    }


def _sign(secret: str, timestamp: str, body: str) -> str:
    mac = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.{body}".encode(),
        hashlib.sha256,
    )
    return f"sha256={mac.hexdigest()}"


async def _pin_resolve(host: str) -> list[str]:
    """Resolver for the pinned fetcher. Looks ``_resolve_addresses`` up at call
    time so the pre-check and the pin share one resolver (and one test seam).
    No answer comes back empty, which the fetcher fails closed on."""
    return await _resolve_addresses(host) or []


def _new_fetcher():
    from pocketpaw_ee.sites.safe_fetch import SafeFetcher

    return SafeFetcher(
        total_byte_cap=1 << 62,
        per_fetch_cap=_RESPONSE_CAP_BYTES,
        timeout_sec=_DELIVERY_TIMEOUT_SECONDS,
        user_agent=_USER_AGENT,
        resolver=_pin_resolve,
    )


async def _mark_unsafe(webhook: AuditWebhook, message: str) -> None:
    webhook.failure_count += 1
    webhook.last_status = None
    webhook.last_error = f"unsafe url: {message}"[:500]
    webhook.last_delivery_at = datetime.now(UTC)
    webhook.enabled = False  # never retry — the URL itself is the problem
    await webhook.save()


async def _deliver_one(
    webhook: AuditWebhook,
    body: str,
    timestamp: str,
    fetcher: Any,
) -> None:
    from pocketpaw_ee.cloud._core.errors import ValidationError

    # Re-check at delivery time so a hostname that flipped to a private
    # IP after create (DNS rebinding, takeover) can't leak signed events.
    try:
        await _validate_url_safety(webhook.url)
    except Forbidden as exc:
        await _mark_unsafe(webhook, exc.message)
        return

    signature = _sign(_decrypt_secret(webhook.secret), timestamp, body)
    try:
        # Pinned POST: the fetcher resolves again, rejects the target if ANY
        # address is non-public, and connects to the address it checked.
        resp = await fetcher.post(
            webhook.url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Paw-Audit-Timestamp": timestamp,
                "X-Paw-Audit-Signature": signature,
            },
        )
    except ValidationError as exc:
        await _mark_unsafe(webhook, exc.message)
        return
    except Exception as exc:
        webhook.failure_count += 1
        webhook.last_status = None
        webhook.last_error = str(exc)[:500]
        webhook.last_delivery_at = datetime.now(UTC)
        if webhook.failure_count >= _FAILURE_DISABLE_THRESHOLD:
            webhook.enabled = False
        await webhook.save()
        return

    webhook.last_delivery_at = datetime.now(UTC)
    webhook.last_status = resp.status
    if 200 <= resp.status < 300:
        webhook.failure_count = 0
        webhook.last_error = None
    else:
        webhook.failure_count += 1
        webhook.last_error = f"http {resp.status}"
        if webhook.failure_count >= _FAILURE_DISABLE_THRESHOLD:
            webhook.enabled = False
    await webhook.save()


async def deliver(event: AuditEvent) -> None:
    """Sign + POST the event to every enabled webhook in the workspace.

    Never raises — a delivery failure persists state and returns. Used
    inline by tests; the audit-record fire-and-forget path wraps this in
    ``asyncio.create_task``.
    """
    try:
        hooks = await AuditWebhook.find(
            {"workspace": event.workspace, "enabled": True},
        ).to_list()
        if not hooks:
            return
        payload = _event_payload(event)
        body = json.dumps(payload, default=str)
        timestamp = str(int(time.time()))
        # The URL here is SUPPLIED BY THE WORKSPACE and the loop below
        # delivers to each hook in turn, so a deliberately slow endpoint
        # delays every later hook: the fetcher's per-request deadline is
        # ``_DELIVERY_TIMEOUT_SECONDS``.
        fetcher = _new_fetcher()
        try:
            for hook in hooks:
                try:
                    await _deliver_one(hook, body, timestamp, fetcher)
                except Exception:
                    logger.warning("audit.webhook delivery crashed for %s", hook.id, exc_info=True)
        finally:
            await fetcher.aclose()
    except Exception:
        logger.warning("audit.webhook deliver fan-out crashed", exc_info=True)


async def _deliver_bounded(event: AuditEvent) -> None:
    async with _get_delivery_slots():
        await deliver(event)


def schedule_delivery(event: AuditEvent) -> None:
    """Fire-and-forget wrapper used by the audit record() path. At most
    ``_MAX_CONCURRENT_DELIVERIES`` run at once; the rest queue on the
    semaphore rather than being dropped (a SIEM feed must not lose events)."""
    try:
        task = asyncio.create_task(_deliver_bounded(event))
    except RuntimeError:
        # No running loop (sync caller, test harness without event loop).
        logger.debug("audit.webhook schedule_delivery: no running loop")
        return
    _inflight_deliveries.add(task)
    task.add_done_callback(_inflight_deliveries.discard)


__all__ = [
    "create_webhook",
    "delete_webhook",
    "deliver",
    "list_webhooks",
    "mint_secret",
    "rotate_secret",
    "schedule_delivery",
    "update_webhook",
    "validate_url_safety",
]
