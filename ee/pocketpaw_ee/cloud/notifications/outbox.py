# ee/pocketpaw_ee/cloud/notifications/outbox.py
# The external-delivery outbox: the ONLY module that reads or writes
# ``notification_outbox``, and the only one that talks to email, webhook and
# Slack endpoints. Producers call ``enqueue_many``; the sweeper loop calls
# ``process_due``.
#
# Claim: one atomic ``find_one_and_update`` moves a due row (``pending`` with
# ``next_at <= now``, or ``sending`` whose ``lease_until`` lapsed) to
# ``sending`` with a fresh lease and ``claim_id``, and bumps ``attempts``. Two
# app instances can therefore never send the same row at once; a finish is
# written only when the row still carries our ``claim_id``.
#
# Outcome per send: ``sent``; ``retry`` (transport error, 429/5xx, any non-2xx
# from a webhook) -> back to ``pending`` after 1 m, 5 m, 30 m, 2 h, 6 h, then
# ``dead``; ``dead`` straight away for a permanent failure (bad request, unsafe
# or removed webhook, unconfirmed recipient, permanent bounce).
#
# Webhooks: the secret is loaded at send time from the config named by
# ``webhook_ref`` ("workspace:<id>" via notifications.service, "site:<id>" via
# leads.notification_settings) and the URL is SSRF-checked again (DNS
# included). A dead webhook row bumps that config's failure counter, which
# switches the webhook off at 10; a sent row resets it. Lead events carry only
# ``lead_id``: the lead is loaded and serialized when the row is sent.
#
# The sweeper is an app-lifespan task started from ``extensions`` beside the run
# sweeper. ``enqueue_many`` wakes it, so a fresh row goes out in well under the
# 15 s tick; without a running sweeper (tests, CLI) rows wait for ``process_due``.

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pymongo import ReturnDocument

from pocketpaw_ee.cloud.models.notification_outbox import NotificationOutboxItem

logger = logging.getLogger(__name__)

BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 1800, 7200, 21600)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1
LEASE_SECONDS = 120
SWEEP_INTERVAL_SECONDS = 15.0
WEBHOOK_DISABLE_THRESHOLD = 10
_HTTP_TIMEOUT_SECONDS = 10.0
_MAX_ERROR_CHARS = 500
_BATCH_LIMIT = 100

_sweeper_task: asyncio.Task[None] | None = None
_wake_event: asyncio.Event | None = None


@dataclass
class Outcome:
    status: Literal["sent", "retry", "dead"]
    error: str = ""
    # False when a dead webhook row says nothing about the endpoint's health
    # (its config was removed or changed), so it doesn't count toward auto-disable.
    counts_against_webhook: bool = True


def _now() -> datetime:
    return datetime.now(UTC)


def backoff_after(attempts: int) -> timedelta | None:
    """Delay before the next try after ``attempts`` failed tries, or None when
    the row has used its last attempt and is dead."""
    if attempts < 1 or attempts > len(BACKOFF_SECONDS):
        return None
    return timedelta(seconds=BACKOFF_SECONDS[attempts - 1])


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


async def enqueue_many(rows: list[dict[str, Any]]) -> list[NotificationOutboxItem]:
    """Insert one pending row per dict (workspace, kind, sink, target, payload,
    optional webhook_ref) and wake the sweeper."""
    if not rows:
        return []
    now = _now()
    docs = [NotificationOutboxItem(next_at=now, created_at=now, **row) for row in rows]
    for doc in docs:
        await doc.insert()
    wake()
    return docs


async def enqueue(**row: Any) -> NotificationOutboxItem:
    return (await enqueue_many([row]))[0]


# ---------------------------------------------------------------------------
# Claim / finish
# ---------------------------------------------------------------------------


async def claim_one(
    *, now: datetime | None = None, lease_seconds: int = LEASE_SECONDS
) -> NotificationOutboxItem | None:
    """Atomically claim the oldest due row, or None when nothing is due."""
    now = now or _now()
    coll = NotificationOutboxItem.get_pymongo_collection()
    raw = await coll.find_one_and_update(
        {
            "$or": [
                {"status": "pending", "next_at": {"$lte": now}},
                {"status": "sending", "lease_until": {"$lte": now}},
            ]
        },
        {
            "$set": {
                "status": "sending",
                "lease_until": now + timedelta(seconds=lease_seconds),
                "claim_id": uuid.uuid4().hex,
            },
            "$inc": {"attempts": 1},
        },
        sort=[("next_at", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if raw is None:
        return None
    return NotificationOutboxItem.model_validate({**raw, "id": raw["_id"]})


async def _finish(item: NotificationOutboxItem, outcome: Outcome, now: datetime) -> str:
    """Write the outcome if we still hold the claim. Returns the final status."""
    error = outcome.error[:_MAX_ERROR_CHARS] or None
    if outcome.status == "sent":
        update: dict[str, Any] = {
            "status": "sent",
            "finished_at": now,
            "lease_until": None,
            "last_error": None,
        }
    else:
        delay = backoff_after(item.attempts) if outcome.status == "retry" else None
        if delay is None:
            update = {
                "status": "dead",
                "finished_at": now,
                "lease_until": None,
                "last_error": error,
            }
        else:
            update = {
                "status": "pending",
                "next_at": now + delay,
                "lease_until": None,
                "last_error": error,
            }
    coll = NotificationOutboxItem.get_pymongo_collection()
    result = await coll.update_one(
        {"_id": item.id, "status": "sending", "claim_id": item.claim_id}, {"$set": update}
    )
    if getattr(result, "matched_count", 1) == 0:
        logger.info("outbox row %s lost its claim before finishing", item.id)
        return "lost"
    return update["status"]


# ---------------------------------------------------------------------------
# Senders
# ---------------------------------------------------------------------------


async def _check_url(url: str) -> str:
    """ "" when ``url`` is safe to POST to now, else the reason."""
    from pocketpaw_ee.cloud._core.errors import Forbidden
    from pocketpaw_ee.cloud.notifications.delivery import validate_webhook_url

    try:
        await validate_webhook_url(url)
    except Forbidden as exc:
        return f"unsafe url: {exc.message}"
    return ""


async def _webhook_target(item: NotificationOutboxItem) -> tuple[str, str] | None:
    """(url, secret) currently configured for the row's ``webhook_ref``."""
    kind, _, ident = item.webhook_ref.partition(":")
    if kind == "workspace":
        from pocketpaw_ee.cloud.notifications import service as notifications_service

        return await notifications_service.webhook_target(ident)
    if kind == "site":
        from pocketpaw_ee.cloud.leads import notification_settings

        return await notification_settings.webhook_target(item.workspace, ident)
    return None


async def _record_webhook_result(item: NotificationOutboxItem, ok: bool) -> None:
    kind, _, ident = item.webhook_ref.partition(":")
    try:
        if kind == "workspace":
            from pocketpaw_ee.cloud.notifications import service as notifications_service

            await notifications_service.record_webhook_result(ident, ok=ok)
        elif kind == "site":
            from pocketpaw_ee.cloud.leads import notification_settings

            await notification_settings.record_webhook_result(item.workspace, ident, ok=ok)
    except Exception:
        logger.warning("could not record webhook result for %s", item.webhook_ref, exc_info=True)


async def _lead_data(item: NotificationOutboxItem) -> dict[str, Any] | None:
    from pocketpaw_ee.cloud.leads import service as leads_service

    return await leads_service.lead_payload(item.workspace, str(item.payload.get("lead_id") or ""))


async def _send_webhook(item: NotificationOutboxItem, client: httpx.AsyncClient) -> Outcome:
    from pocketpaw_ee.cloud.notifications import webhook_signing

    target = await _webhook_target(item)
    if target is None or target[0] != item.target:
        return Outcome(
            "dead", "webhook removed, changed or switched off", counts_against_webhook=False
        )
    url, secret = target
    if reason := await _check_url(url):
        return Outcome("dead", reason)
    payload = item.payload
    if "data" in payload:
        data = payload["data"]
    elif payload.get("lead_id"):
        data = await _lead_data(item)
        if data is None:
            return Outcome("dead", "lead not found")
    else:
        return Outcome("dead", "no event data")
    event = webhook_signing.build_event(
        event_id=str(payload.get("event_id") or item.id),
        event_type=str(payload.get("event_type") or item.kind),
        created_at=str(payload.get("created_at") or item.created_at.isoformat()),
        data=data,
    )
    body = webhook_signing.encode_event(event)
    try:
        resp = await client.post(
            url, content=body, headers=webhook_signing.sign_headers(secret, body)
        )
    except Exception as exc:  # noqa: BLE001 — any transport failure retries
        return Outcome("retry", f"transport: {type(exc).__name__}")
    if 200 <= resp.status_code < 300:
        return Outcome("sent")
    return Outcome("retry", f"http {resp.status_code}")


async def _send_slack(item: NotificationOutboxItem, client: httpx.AsyncClient) -> Outcome:
    if reason := await _check_url(item.target):
        return Outcome("dead", reason)
    try:
        resp = await client.post(item.target, json=item.payload)
    except Exception as exc:  # noqa: BLE001
        return Outcome("retry", f"transport: {type(exc).__name__}")
    if 200 <= resp.status_code < 300:
        return Outcome("sent")
    return Outcome("retry", f"http {resp.status_code}")


async def _render_email(item: NotificationOutboxItem):
    from pocketpaw_ee.cloud.notifications import email as email_mod

    p = item.payload
    template = p.get("template")
    if template == "lead":
        lead = await _lead_data(item)
        return email_mod.render_lead_email(lead) if lead is not None else None
    if template == "notification":
        return email_mod.render_notification_email(
            title=str(p.get("title") or ""),
            body=str(p.get("body") or ""),
            link=str(p.get("link") or email_mod.app_base_url()),
            footer_url=str(p.get("footer_url") or email_mod.workspace_settings_url()),
            site_name=str(p.get("site_name") or ""),
        )
    if template == "confirm":
        return email_mod.render_confirm_email(
            site_name=str(p.get("site_name") or ""),
            confirm_url=str(p.get("confirm_url") or ""),
            footer_url=str(p.get("footer_url") or email_mod.workspace_settings_url()),
        )
    if template == "test":
        return email_mod.render_test_email(
            site_name=str(p.get("site_name") or ""),
            footer_url=str(p.get("footer_url") or email_mod.workspace_settings_url()),
        )
    return None


def _site_settings():
    from pocketpaw_ee.cloud.leads import notification_settings

    return notification_settings


async def _send_email(item: NotificationOutboxItem, client: httpx.AsyncClient) -> Outcome:
    from pocketpaw_ee.cloud.notifications import email as email_mod

    config = email_mod.load_config()
    if config is None:
        return Outcome("dead", "email sink not configured")
    site_ref = str(item.payload.get("site_ref") or "")
    # A per-site recipient must still be on the list and confirmed at send time
    # (the confirm email itself is the one message an unconfirmed address gets).
    if site_ref and item.payload.get("template") != "confirm":
        settings = _site_settings()
        if not await settings.recipient_allowed(item.workspace, site_ref, item.target):
            return Outcome("dead", "recipient no longer allowed")
    rendered = await _render_email(item)
    if rendered is None:
        return Outcome("dead", "nothing to render")
    result = await email_mod.send_email(
        email_mod.EmailMessage(
            to=[item.target],
            subject=rendered.subject,
            html=rendered.html,
            text=rendered.text,
            reply_to=rendered.reply_to,
        ),
        config=config,
        client=client,
    )
    if result.outcome == "retry":
        return Outcome("retry", result.error)
    if result.outcome == "permanent":
        return Outcome("dead", result.error)
    bounced = {a.lower() for a in result.permanent_bounces}
    if item.target.lower() in bounced:
        if site_ref:
            await _site_settings().record_bounce(item.workspace, site_ref, item.target)
        return Outcome("dead", "permanent bounce")
    return Outcome("sent")


_SENDERS = {"email": _send_email, "webhook": _send_webhook, "slack": _send_slack}


async def deliver(item: NotificationOutboxItem, client: httpx.AsyncClient) -> Outcome:
    """Send one claimed row. Never raises: a crash is a retry."""
    try:
        return await _SENDERS[item.sink](item, client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("outbox send crashed for %s", item.id, exc_info=True)
        return Outcome("retry", f"crash: {type(exc).__name__}")


async def process_due(*, now: datetime | None = None, limit: int = _BATCH_LIMIT) -> int:
    """Claim and send due rows until none are left (or ``limit``). Returns how
    many rows were handled. Never raises."""
    handled = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS)) as client:
            while handled < limit:
                item = await claim_one(now=now)
                if item is None:
                    break
                outcome = await deliver(item, client)
                status = await _finish(item, outcome, now or _now())
                if item.sink == "webhook" and status in ("sent", "dead"):
                    if status == "sent" or outcome.counts_against_webhook:
                        await _record_webhook_result(item, ok=status == "sent")
                handled += 1
    except Exception:
        logger.warning("outbox sweep crashed", exc_info=True)
    return handled


# ---------------------------------------------------------------------------
# Sweeper lifecycle (app lifespan, see ``pocketpaw_ee.extensions``)
# ---------------------------------------------------------------------------


def wake() -> None:
    """Nudge a running sweeper to look now. No-op without one."""
    if _wake_event is not None:
        _wake_event.set()


async def _sweeper_loop(interval: float) -> None:
    assert _wake_event is not None
    logger.info("notification outbox sweeper started (interval=%.0fs)", interval)
    while True:
        # A full batch means more may be due: go again before sleeping.
        while await process_due() >= _BATCH_LIMIT:
            pass
        with suppress(TimeoutError):
            await asyncio.wait_for(_wake_event.wait(), timeout=interval)
        _wake_event.clear()


async def start_outbox_sweeper(interval: float = SWEEP_INTERVAL_SECONDS) -> None:
    global _sweeper_task, _wake_event
    if _sweeper_task is not None:
        return
    _wake_event = asyncio.Event()
    _sweeper_task = asyncio.create_task(_sweeper_loop(interval))


async def stop_outbox_sweeper() -> None:
    global _sweeper_task, _wake_event
    if _sweeper_task is not None:
        _sweeper_task.cancel()
        with suppress(asyncio.CancelledError):
            await _sweeper_task
    _sweeper_task = None
    _wake_event = None


__all__ = [
    "BACKOFF_SECONDS",
    "MAX_ATTEMPTS",
    "Outcome",
    "WEBHOOK_DISABLE_THRESHOLD",
    "backoff_after",
    "claim_one",
    "deliver",
    "enqueue",
    "enqueue_many",
    "process_due",
    "start_outbox_sweeper",
    "stop_outbox_sweeper",
    "wake",
]
