# ee/pocketpaw_ee/cloud/notifications/outbox.py
# The external-delivery outbox: the ONLY module that reads or writes
# ``notification_outbox``, and the only one that talks to email, webhook and
# Slack endpoints. Producers call ``enqueue_many``; the sweeper calls ``process_due``.
#
# Claim: one atomic ``find_one_and_update`` moves a due row (``pending`` with
# ``next_at <= now``, or ``sending`` whose lease lapsed) to ``sending`` with a
# fresh lease and ``claim_id``, and bumps ``attempts``. Two app instances never
# send the same row at once; a finish is written only while we hold the claim.
# A row that fails to parse after the claim is marked dead by ``_id``.
#
# Throughput and isolation: rows are worked in two LANES with their own claims
# and workers (email: 4, webhook + Slack: 8), so a slow webhook can't hold mail
# back. Every send runs under a hard ``SEND_DEADLINE_SECONDS`` (well under the
# lease); hitting it is an ordinary failure.
#
# Outcome per send: ``sent``; ``retry`` (transport error, deadline, 429/5xx, any
# non-2xx from a webhook, a Cloudflare 401/403) -> back to ``pending`` after 1 m,
# 5 m, 30 m, 2 h, 6 h, then ``dead``; ``dead`` straight away for a permanent
# failure (bad request, unsafe or removed webhook, unconfirmed recipient,
# permanent bounce). A Cloudflare 401/403 is logged at error level and rings the
# workspace admins once a day (an atomic marker, ``claim_marker``).
#
# Webhooks and Slack go out through ``sites.safe_fetch.SafeFetcher.post``: DNS is
# resolved and checked at send time (failing closed), and the connection is
# pinned to the checked address, so rebinding can't redirect it. A webhook row's
# target is re-read at send time from ``webhook_ref``: "workspace:<id>", or
# "site:<site_id>:<webhook_id>" for one site destination ("site:<site_id>", from
# rows queued before destinations existed, means the ``legacy`` one). A json
# target gets the signed envelope (a workspace webhook saved before signing
# existed has no secret and goes unsigned); a chat target (Slack, Discord, Teams,
# Google Chat) gets ``webhook_formats.render`` with its template, unsigned, and
# skips ``lead.updated``. Health is counted per destination. Lead events carry
# only ``lead_id``; the lead is loaded at send time. Rows with ``legacy`` get
# those deprecated flat fields merged into the body's top level.
#
# The sweeper is an app-lifespan task started from ``extensions``. ``enqueue_many``
# wakes it; without a running sweeper (tests, CLI) rows wait for ``process_due``.
#
# ``whatsapp`` (partner leads) is worked in the webhook lane through the
# platform MSG91 account (``growth.msg91``): no credentials -> dead; the
# client's number must still be the opted-in target at send time (a failed
# lookup retries). An MSG91 4xx (not 429) or a rejected send is dead, anything
# else retries; only the error code and status are stored. The text is one line
# built from the lead at send time, formatting marks stripped and links broken,
# capped at ``WHATSAPP_BODY_CAP``. ``count_recent`` filters by sink / target for
# the per-number daily cap.

from __future__ import annotations

import asyncio
import logging
import re
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
SEND_DEADLINE_SECONDS = 30.0
SWEEP_INTERVAL_SECONDS = 15.0
WEBHOOK_DISABLE_THRESHOLD = 10
_HTTP_TIMEOUT_SECONDS = 10.0
_MAX_ERROR_CHARS = 500
_BATCH_LIMIT = 100
_WEBHOOK_RESPONSE_CAP = 1024 * 1024
_USER_AGENT = "PocketPaw-Webhooks/1.0 (+https://pocketpaw.dev)"
EMAIL_FAILING_KIND = "owner_email_failing"
# Cap on the one template body variable. Meta's limit (1024) is the whole
# substituted body, so leave room for the template's short fixed prefix.
WHATSAPP_BODY_CAP = 900

# (sinks claimed by the lane, workers in the lane)
LANES: tuple[tuple[tuple[str, ...], int], ...] = (
    (("email",), 4),
    (("webhook", "slack", "whatsapp"), 8),
)

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
# Enqueue / counts
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


async def count_recent(
    *,
    workspace: str,
    kind: str,
    since: datetime,
    sink: str | None = None,
    target: str | None = None,
) -> int:
    """Rows of ``kind`` queued for ``workspace`` since ``since`` (rate limits),
    optionally only those of one ``sink`` / ``target``."""
    query: dict[str, Any] = {"workspace": workspace, "kind": kind, "created_at": {"$gte": since}}
    if sink is not None:
        query["sink"] = sink
    if target is not None:
        query["target"] = target
    return await NotificationOutboxItem.find(query).count()


async def claim_marker(key: str, interval: timedelta, *, now: datetime | None = None) -> bool:
    """Atomic once-per-``interval`` gate: True (and the marker is stamped) when
    ``key`` wasn't done within ``interval``; False otherwise. The unique key
    means two concurrent callers can't both get True."""
    from pymongo.errors import DuplicateKeyError

    from pocketpaw_ee.cloud.models.notification_outbox import NotificationRateMarker

    now = now or _now()
    coll = NotificationRateMarker.get_pymongo_collection()
    try:
        await coll.find_one_and_update(
            {"key": key, "at": {"$lt": now - interval}},
            {"$set": {"at": now}},
            upsert=True,
        )
    except DuplicateKeyError:
        return False
    return True


# ---------------------------------------------------------------------------
# Claim / finish
# ---------------------------------------------------------------------------


async def claim_one(
    *,
    now: datetime | None = None,
    lease_seconds: int = LEASE_SECONDS,
    sinks: tuple[str, ...] | None = None,
) -> NotificationOutboxItem | None:
    """Atomically claim the oldest due row (of ``sinks`` when given), or None
    when nothing is due. A claimed row that doesn't parse is marked dead and
    skipped rather than crashing the sweep."""
    now = now or _now()
    coll = NotificationOutboxItem.get_pymongo_collection()
    query: dict[str, Any] = {
        "$or": [
            {"status": "pending", "next_at": {"$lte": now}},
            {"status": "sending", "lease_until": {"$lte": now}},
        ]
    }
    if sinks is not None:
        query["sink"] = {"$in": list(sinks)}
    while True:
        raw = await coll.find_one_and_update(
            query,
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
        try:
            return NotificationOutboxItem.model_validate({**raw, "id": raw["_id"]})
        except Exception as exc:  # noqa: BLE001 — one bad row must not stop the sweep
            logger.warning("outbox row %s is malformed; marking it dead", raw.get("_id"))
            await coll.update_one(
                {"_id": raw["_id"]},
                {
                    "$set": {
                        "status": "dead",
                        "finished_at": now,
                        "lease_until": None,
                        "last_error": f"malformed row: {type(exc).__name__}",
                    }
                },
            )


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
# HTTP (webhook + Slack) through the pinned SafeFetcher
# ---------------------------------------------------------------------------


async def _resolve(host: str) -> list[str]:
    """DNS for the pinned fetcher. Reads the audit webhooks' resolver at call
    time (one resolver for every webhook path); no answer fails closed."""
    from pocketpaw_ee.cloud.audit import webhooks as audit_webhooks

    ips = await audit_webhooks._resolve_addresses(host)
    if not ips:
        raise OSError(f"no addresses for {host}")
    return ips


def _new_fetcher():
    from pocketpaw_ee.sites.safe_fetch import SafeFetcher

    return SafeFetcher(
        total_byte_cap=1 << 62,
        per_fetch_cap=_WEBHOOK_RESPONSE_CAP,
        timeout_sec=_HTTP_TIMEOUT_SECONDS,
        user_agent=_USER_AGENT,
        resolver=_resolve,
    )


async def _post(fetcher, url: str, body: str, headers: dict[str, str]) -> Outcome:
    """POST through the pinned fetcher and map the result to an Outcome."""
    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.cloud.notifications.delivery import is_safe_webhook_url
    from pocketpaw_ee.sites.safe_fetch import FetchError

    if not is_safe_webhook_url(url):
        return Outcome("dead", "unsafe url: not an https URL to a public host")
    try:
        result = await fetcher.post(url, content=body, headers=headers)
    except ValidationError as exc:
        return Outcome("dead", f"unsafe url: {exc.message}")
    except FetchError as exc:
        # DNS failure (or an oversized answer): nothing was sent to a host we
        # couldn't check. Try again later.
        return Outcome("retry", f"fetch: {exc.code}")
    except Exception as exc:  # noqa: BLE001 — any transport failure retries
        return Outcome("retry", f"transport: {type(exc).__name__}")
    if 200 <= result.status < 300:
        return Outcome("sent")
    return Outcome("retry", f"http {result.status}")


@dataclass
class _Target:
    url: str
    secrets: list[str]
    platform: str = "json"
    template: Any = None
    site_id: str = ""
    site_name: str = ""


async def _webhook_target(item: NotificationOutboxItem) -> _Target | None:
    """The destination currently configured for the row's ``webhook_ref``."""
    kind, _, ident = item.webhook_ref.partition(":")
    if kind == "workspace":
        from pocketpaw_ee.cloud.notifications import service as notifications_service

        found = await notifications_service.webhook_target(ident)
        return _Target(url=found[0], secrets=list(found[1])) if found else None
    if kind == "site":
        from pocketpaw_ee.cloud.leads import webhook_destinations

        site_id, webhook_id = webhook_destinations.parse_ref(ident)
        dest = await webhook_destinations.target(item.workspace, site_id, webhook_id)
        if dest is None:
            return None
        return _Target(
            url=dest.url,
            secrets=dest.secrets,
            platform=dest.platform,
            template=dest.template,
            site_id=dest.site_id,
            site_name=dest.site_name,
        )
    return None


async def _record_webhook_result(item: NotificationOutboxItem, ok: bool) -> None:
    kind, _, ident = item.webhook_ref.partition(":")
    try:
        if kind == "workspace":
            from pocketpaw_ee.cloud.notifications import service as notifications_service

            await notifications_service.record_webhook_result(ident, ok=ok)
        elif kind == "site":
            from pocketpaw_ee.cloud.leads import webhook_destinations

            site_id, webhook_id = webhook_destinations.parse_ref(ident)
            await webhook_destinations.record_result(item.workspace, site_id, webhook_id, ok=ok)
    except Exception:
        logger.warning("could not record webhook result for %s", item.webhook_ref, exc_info=True)


async def _lead_data(item: NotificationOutboxItem) -> dict[str, Any] | None:
    from pocketpaw_ee.cloud.leads import service as leads_service

    return await leads_service.lead_payload(item.workspace, str(item.payload.get("lead_id") or ""))


async def _send_webhook(item: NotificationOutboxItem, fetcher) -> Outcome:
    from pocketpaw_ee.cloud.notifications import webhook_signing

    target = await _webhook_target(item)
    if target is None or target.url != item.target:
        return Outcome(
            "dead", "webhook removed, changed or switched off", counts_against_webhook=False
        )
    url, secrets = target.url, target.secrets
    payload = item.payload
    event_type = str(payload.get("event_type") or item.kind)
    if target.platform != "json" and event_type == "lead.updated":
        return Outcome("dead", "chat destinations skip lead.updated", counts_against_webhook=False)
    if "data" in payload:
        data = payload["data"]
    elif payload.get("lead_id"):
        data = await _lead_data(item)
        if data is None:
            return Outcome("dead", "lead not found")
    else:
        return Outcome("dead", "no event data")
    created_at = str(payload.get("created_at") or item.created_at.isoformat())
    if target.platform != "json":
        return await _post(fetcher, url, *_chat_body(item, target, event_type, data, created_at))
    event = webhook_signing.build_event(
        event_id=str(payload.get("event_id") or item.id),
        event_type=event_type,
        created_at=created_at,
        data=data,
    )
    legacy = payload.get("legacy")
    if isinstance(legacy, dict):
        # Workspace webhook only: the deprecated flat fields, never overriding
        # an envelope key.
        event = {**event, **{k: v for k, v in legacy.items() if k not in event}}
    body = webhook_signing.encode_event(event)
    headers = (
        webhook_signing.sign_headers(secrets, body)
        if secrets
        else {"Content-Type": "application/json"}  # pre-signing webhook: as before
    )
    return await _post(fetcher, url, body, headers)


def _chat_body(
    item: NotificationOutboxItem, target: _Target, event_type: str, data: Any, created_at: str
) -> tuple[str, dict[str, str]]:
    """(body, headers) for a chat-app destination, rendered from its template."""
    import json

    from pocketpaw_ee.cloud.notifications import email as email_mod
    from pocketpaw_ee.cloud.notifications import webhook_formats

    link = str(item.payload.get("link") or "")
    if not link and target.site_id:
        lead_id = str(item.payload.get("lead_id") or "")
        link = email_mod.lead_url(target.site_id, lead_id)
    body, headers = webhook_formats.render(
        target.platform,
        event_type,
        data if isinstance(data, dict) else {},
        target.template,
        site_name=target.site_name,
        link=link,
        created_at=created_at,
    )
    return json.dumps(body, separators=(",", ":"), default=str), headers


async def _send_slack(item: NotificationOutboxItem, fetcher) -> Outcome:
    import json

    from pocketpaw_ee.cloud.notifications import service as notifications_service

    if await notifications_service.slack_target(item.workspace) != item.target:
        return Outcome("dead", "slack removed, changed or switched off")
    body = json.dumps(item.payload)
    return await _post(fetcher, item.target, body, {"Content-Type": "application/json"})


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


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


async def _warn_email_failing(workspace_id: str, error: str) -> None:
    """Cloudflare refused our credentials. That's a platform problem, not the
    workspace's: log it at error level for operators, and tell the workspace's
    owner/admins (at most once a day, race-safe across workers) that lead email
    is failing and the platform team knows. Never raises."""
    logger.error(
        "owner email: Cloudflare rejected the send credentials (%s); check "
        "POCKETPAW_CF_EMAIL_API_TOKEN and that sending is enabled for the domain",
        error,
    )
    try:
        if not await claim_marker(f"email_failing:{workspace_id}", timedelta(days=1)):
            return
        from pocketpaw_ee.cloud.notifications import service as notifications_service
        from pocketpaw_ee.cloud.workspace import service as workspace_service

        admins = await workspace_service.list_admin_ids(workspace_id)
        if admins:
            await notifications_service.create_many(
                workspace_id=workspace_id,
                recipients=admins,
                kind=EMAIL_FAILING_KIND,
                title="Lead email is delayed",
                body="Lead notification email can't be delivered right now. The platform "
                "team has been alerted, and queued email will be retried. Leads are "
                "still saved and shown in the app.",
                deliver_external=False,
            )
    except Exception:
        logger.warning("could not raise the email-failing notice", exc_info=True)


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
    if result.auth_failure:
        await _warn_email_failing(item.workspace, result.error)
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


# ---------------------------------------------------------------------------
# WhatsApp (partner leads)
# ---------------------------------------------------------------------------


def _one_line(value: Any, cap: int) -> str:
    # Meta rejects template variables with newlines, tabs or long space runs.
    text = " ".join(str(value or "").split())
    return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"


_WA_MARKS = re.compile(r"[*_~`]")
_WA_CONTACT_MARKS = re.compile(r"[*~`]")  # "_" is legal in an email address
_LINK = re.compile(r"\b(h)tt(ps?://)", re.IGNORECASE)


def _defang(text: str, marks: re.Pattern[str] = _WA_MARKS) -> str:
    """Visitor text must not format the message or become a tappable link."""
    return _LINK.sub(r"\1xx\2", marks.sub("", text))


def whatsapp_lead_text(lead: dict[str, Any]) -> str:
    """The partner-lead WhatsApp text for ``lead`` (``leads.service.lead_payload``),
    at most ``WHATSAPP_BODY_CAP`` characters. Contact first, then the message
    gets whatever room is left."""
    site = _one_line(lead.get("site_name"), 80) or "your site"
    name = _one_line(_defang(str(lead.get("name") or "")), 120) or "a visitor"
    raw_contact = str(lead.get("phone") or "").strip() or str(lead.get("email") or "")
    contact = _one_line(_defang(raw_contact, _WA_CONTACT_MARKS), 120)
    head = f"New enquiry for {site} via Paw Sites by PocketPaw: {name}"
    tail = f" Contact: {contact}" if contact else ""
    room = WHATSAPP_BODY_CAP - len(head) - len(tail) - len(" — ")
    message = _one_line(_defang(str(lead.get("message") or "")), room) if room > 1 else ""
    return (head + (f" — {message}" if message else "") + tail)[:WHATSAPP_BODY_CAP]


async def _send_whatsapp(item: NotificationOutboxItem) -> Outcome:
    from pocketpaw_ee.cloud.growth import msg91

    creds = msg91.resolve_platform_credentials()
    if creds is None:
        return Outcome("dead", "whatsapp sink not configured")
    settings = _site_settings()
    site = await settings.find_site(item.workspace, str(item.payload.get("site_ref") or ""))
    # strict: a lookup that fails raises (-> retry) instead of reading as "no consent".
    if await settings.partner_whatsapp_target(item.workspace, site, strict=True) != item.target:
        return Outcome("dead", "recipient no longer allowed")
    lead = await _lead_data(item)
    if lead is None:
        return Outcome("dead", "lead not found")
    try:
        await msg91.Msg91WhatsAppClient(creds).send_template(
            to_number=item.target, body_text=whatsapp_lead_text(lead)
        )
    except msg91.Msg91Error as exc:
        error = f"msg91: {exc.code}" + (f" {exc.status}" if exc.status else "")
        permanent = exc.code == "msg91.rejected" or (
            exc.status is not None and 400 <= exc.status < 500 and exc.status != 429
        )
        return Outcome("dead" if permanent else "retry", error)
    return Outcome("sent")


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


async def deliver(item: NotificationOutboxItem, client: httpx.AsyncClient, fetcher) -> Outcome:
    """Send one claimed row under the hard deadline. Never raises: a crash or a
    blown deadline is a retry."""
    try:
        async with asyncio.timeout(SEND_DEADLINE_SECONDS):
            if item.sink == "email":
                return await _send_email(item, client)
            if item.sink == "webhook":
                return await _send_webhook(item, fetcher)
            if item.sink == "slack":
                return await _send_slack(item, fetcher)
            if item.sink == "whatsapp":
                return await _send_whatsapp(item)
            return Outcome("dead", f"unknown sink {item.sink!r}")
    except TimeoutError:
        return Outcome("retry", f"deadline: no answer in {SEND_DEADLINE_SECONDS:.0f}s")
    except Exception as exc:  # noqa: BLE001
        logger.warning("outbox send crashed for %s", item.id, exc_info=True)
        return Outcome("retry", f"crash: {type(exc).__name__}")


async def _handle(item: NotificationOutboxItem, client, fetcher, now: datetime | None) -> None:
    outcome = await deliver(item, client, fetcher)
    status = await _finish(item, outcome, now or _now())
    if item.sink == "webhook" and status in ("sent", "dead"):
        if status == "sent" or outcome.counts_against_webhook:
            await _record_webhook_result(item, ok=status == "sent")


async def process_due(*, now: datetime | None = None, limit: int = _BATCH_LIMIT) -> int:
    """Claim and send due rows across the lanes until none are left (or
    ``limit``). Returns how many rows were handled. Never raises."""
    handled = 0

    async def worker(sinks: tuple[str, ...], client, fetcher) -> None:
        nonlocal handled
        while handled < limit:
            item = await claim_one(now=now, sinks=sinks)
            if item is None:
                return
            handled += 1
            await _handle(item, client, fetcher, now)

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS)) as client:
            fetcher = _new_fetcher()
            try:
                await asyncio.gather(
                    *(worker(sinks, client, fetcher) for sinks, n in LANES for _ in range(n))
                )
            finally:
                await fetcher.aclose()
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
    "LANES",
    "MAX_ATTEMPTS",
    "Outcome",
    "SEND_DEADLINE_SECONDS",
    "WEBHOOK_DISABLE_THRESHOLD",
    "WHATSAPP_BODY_CAP",
    "backoff_after",
    "claim_marker",
    "claim_one",
    "count_recent",
    "deliver",
    "enqueue",
    "enqueue_many",
    "process_due",
    "start_outbox_sweeper",
    "stop_outbox_sweeper",
    "wake",
    "whatsapp_lead_text",
]
