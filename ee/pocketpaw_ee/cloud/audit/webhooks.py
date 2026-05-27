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
rest with the shared SSO Fernet key; URLs are revalidated per delivery
to catch DNS rebinding mid-flight.
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

import httpx
from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.auth.sso import crypto as _crypto
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.models.audit_webhook import AuditWebhook

logger = logging.getLogger(__name__)

_FAILURE_DISABLE_THRESHOLD = 10
_DELIVERY_TIMEOUT_SECONDS = 5.0

# Why: asyncio.create_task only keeps a weakref; if the event loop GCs the
# task before it runs we silently lose deliveries (and Python logs a
# RuntimeWarning). Holding strong refs in a module-level set keeps them
# alive until done_callback discards.
_inflight_deliveries: set[asyncio.Task[None]] = set()


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
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve_addresses(hostname: str) -> list[str] | None:
    """Return all resolved IP strings, or None if DNS resolution failed.

    Why None on failure: an unresolvable hostname will fail at HTTP time
    anyway; raising here would also break dev/test setups that use made-up
    domains like ``siem.example.com``.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return None
    return [info[4][0] for info in infos]


def _validate_url_safety(url: str) -> None:
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
    addresses = _resolve_addresses(hostname)
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
    _validate_url_safety(url)
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


async def _deliver_one(
    webhook: AuditWebhook,
    body: str,
    timestamp: str,
    client: httpx.AsyncClient,
) -> None:
    # Re-check at delivery time so a hostname that flipped to a private
    # IP after create (DNS rebinding, takeover) can't leak signed events.
    try:
        _validate_url_safety(webhook.url)
    except Forbidden as exc:
        webhook.failure_count += 1
        webhook.last_status = None
        webhook.last_error = f"unsafe url: {exc.message}"[:500]
        webhook.last_delivery_at = datetime.now(UTC)
        webhook.enabled = False  # never retry — the URL itself is the problem
        await webhook.save()
        return

    signature = _sign(_decrypt_secret(webhook.secret), timestamp, body)
    try:
        resp = await client.post(
            webhook.url,
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Paw-Audit-Timestamp": timestamp,
                "X-Paw-Audit-Signature": signature,
            },
            timeout=_DELIVERY_TIMEOUT_SECONDS,
        )
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
    webhook.last_status = resp.status_code
    if 200 <= resp.status_code < 300:
        webhook.failure_count = 0
        webhook.last_error = None
    else:
        webhook.failure_count += 1
        webhook.last_error = f"http {resp.status_code}"
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
        async with httpx.AsyncClient() as client:
            for hook in hooks:
                try:
                    await _deliver_one(hook, body, timestamp, client)
                except Exception:
                    logger.warning("audit.webhook delivery crashed for %s", hook.id, exc_info=True)
    except Exception:
        logger.warning("audit.webhook deliver fan-out crashed", exc_info=True)


def schedule_delivery(event: AuditEvent) -> None:
    """Fire-and-forget wrapper used by the audit record() path."""
    try:
        task = asyncio.create_task(deliver(event))
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
]
