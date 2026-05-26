"""SIEM webhook delivery for workspace audit events (Wave 3 Task 15).

External HTTPS endpoint registry. Each enabled webhook receives a signed
POST per audit event. Signature scheme:

    body = f"{timestamp}.{json_payload}"
    sig  = HMAC-SHA256(secret, body)

Headers:
    X-Paw-Audit-Timestamp: <unix>
    X-Paw-Audit-Signature: sha256=<hex>

Auto-disable after 10 consecutive failures.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.models.audit_webhook import AuditWebhook

logger = logging.getLogger(__name__)

_FAILURE_DISABLE_THRESHOLD = 10
_DELIVERY_TIMEOUT_SECONDS = 5.0


def mint_secret() -> str:
    return secrets.token_urlsafe(32)


def _require_https(url: str) -> None:
    if not url.startswith("https://"):
        raise Forbidden("webhooks.https_required", "Webhook URL must be https://")


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
    _require_https(url)
    secret = mint_secret()
    doc = AuditWebhook(
        workspace=workspace_id,
        url=url,
        secret=secret,
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
    doc.secret = new_secret
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
    signature = _sign(webhook.secret, timestamp, body)
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
        asyncio.create_task(deliver(event))
    except RuntimeError:
        # No running loop (sync caller, test harness without event loop).
        logger.debug("audit.webhook schedule_delivery: no running loop")


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
