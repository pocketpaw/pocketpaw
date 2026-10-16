# ee/pocketpaw_ee/cloud/leads/whatsapp_numbers.py
# A site owner's own WhatsApp numbers for lead alerts
# (``SiteNotificationSettings.whatsapp.numbers``, at most ``MAX_WHATSAPP_NUMBERS``):
# add / remove, the settings wire, the outbox rows a site event turns into, the
# per-site hourly cap, the test send and the send-time number check. Together
# with ``notification_settings`` and ``webhook_destinations`` it is the only
# writer of ``site_notification_settings``; it borrows that module's site loading
# and row key.
#
# A number is E.164 ("+" then 8-15 digits; spaces, dashes, dots and brackets are
# stripped first) and is only stored when the adder ticks consent; the consent
# time is kept with it. The add is ONE conditional ``$push`` whose filter is the
# cap and the dedupe. Adding the FIRST number also switches the
# ``whatsapp_owner`` sink on for every event in that same update (an event the
# row doesn't list yet gets the defaults plus ``whatsapp_owner``). Removing a
# number leaves the routing alone; with no numbers the sink just sends nothing.
#
# Sending goes through ``notifications.whatsapp_notify`` (mock mode without
# Meta credentials). Each message may be paid for, so a site queues at most
# ``HOURLY_CAP`` ``whatsapp_owner`` rows per rolling hour, test sends included,
# counted on the outbox; past it, rows are skipped with one warning per event.
# The partner-lead ``whatsapp`` sink (MSG91) is separate and unaffected.

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw_ee.cloud._core.errors import NotFound, RateLimited, ValidationError
from pocketpaw_ee.cloud.leads import notification_settings as ns
from pocketpaw_ee.cloud.models.lead_notifications import (
    DEFAULT_EVENT_SINKS,
    LEAD_EVENTS,
    MAX_WHATSAPP_NUMBERS,
    LeadWhatsAppNumber,
    SiteNotificationSettings,
)

logger = logging.getLogger(__name__)

SINK = "whatsapp_owner"
HOURLY_CAP = 30
TEST_KIND = "lead_notifications_whatsapp_test"
_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_SEPARATORS = re.compile(r"[\s\-.()]")


def _now() -> datetime:
    return datetime.now(UTC)


def normalize(value: str) -> str:
    """The E.164 form of ``value``, or ``ValidationError invalid_phone``."""
    raw = _SEPARATORS.sub("", str(value or ""))
    if raw.startswith("00"):
        raw = "+" + raw[2:]
    if not _E164.match(raw):
        raise ValidationError(
            "lead_notifications.invalid_phone",
            "Enter the number in international format, like +14155550123.",
        )
    return raw


def numbers(settings: SiteNotificationSettings) -> list[str]:
    return [n.e164 for n in settings.whatsapp.numbers]


def wire(settings: SiteNotificationSettings) -> dict[str, Any]:
    from pocketpaw_ee.cloud.notifications import whatsapp_notify

    return {
        "numbers": [
            {"e164": n.e164, "added_at": n.added_at, "consent_at": n.consent_at}
            for n in settings.whatsapp.numbers
        ],
        "mode": whatsapp_notify.mode(),
        "send_as": whatsapp_notify.send_as(),
        "max_numbers": MAX_WHATSAPP_NUMBERS,
    }


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


async def add(site, e164: str, *, consent: bool, added_by: str = "") -> LeadWhatsAppNumber:
    """Store one consented number (see the module header for the first-number
    routing switch)."""
    if consent is not True:
        raise ValidationError(
            "lead_notifications.whatsapp_consent_required",
            "Confirm that this number may get lead messages on WhatsApp.",
        )
    number = normalize(e164)
    await ns._ensure_row(site)
    now = _now()
    record = LeadWhatsAppNumber(e164=number, added_at=now, added_by=added_by, consent_at=now)
    for _ in range(3):
        current = await ns.settings_for(site)
        listed = numbers(current)
        if number in listed:
            raise ValidationError(
                "lead_notifications.duplicate_number", "That number is already added."
            )
        if len(listed) >= MAX_WHATSAPP_NUMBERS:
            raise _too_many()
        first = not listed
        query: dict[str, Any] = {
            **ns._key(site),
            "whatsapp.numbers.e164": {"$ne": number},
            f"whatsapp.numbers.{MAX_WHATSAPP_NUMBERS - 1}": {"$exists": False},
        }
        update: dict[str, Any] = {"$push": {"whatsapp.numbers": record.model_dump()}}
        if first:
            query["whatsapp.numbers.0"] = {"$exists": False}
            stored = await ns._coll().find_one(ns._key(site), {"events": 1}) or {}
            stored_events = stored.get("events") or {}
            add_to: dict[str, Any] = {}
            set_: dict[str, Any] = {}
            for event in LEAD_EVENTS:
                if event in stored_events:
                    add_to[f"events.{event}"] = SINK
                else:
                    set_[f"events.{event}"] = [*DEFAULT_EVENT_SINKS, SINK]
            if add_to:
                update["$addToSet"] = add_to
            if set_:
                update["$set"] = set_
        result = await ns._coll().update_one(query, update)
        if getattr(result, "modified_count", 0) == 1:
            return record
        # Lost a race (another add landed first): look again.
    raise _too_many()


def _too_many() -> ValidationError:
    return ValidationError(
        "lead_notifications.too_many_numbers",
        f"A site can send WhatsApp alerts to at most {MAX_WHATSAPP_NUMBERS} numbers.",
    )


async def remove(site, e164: str) -> None:
    raw = str(e164 or "").strip()
    if raw and raw[0].isdigit():
        # A "+" that a client sent unencoded can arrive as a space.
        raw = "+" + raw
    try:
        number = normalize(raw)
    except ValidationError:
        raise NotFound("lead_notification_whatsapp_number", e164) from None
    result = await ns._coll().update_one(
        ns._key(site), {"$pull": {"whatsapp.numbers": {"e164": number}}}
    )
    if getattr(result, "modified_count", 0) == 0:
        raise NotFound("lead_notification_whatsapp_number", e164)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


async def _room(site) -> int:
    """How many more rows the site may queue this hour."""
    from pocketpaw_ee.cloud.notifications import outbox

    recent = await outbox.count_recent(
        workspace=site.workspace,
        kind=None,
        since=_now() - timedelta(hours=1),
        sink=SINK,
        site_ref=str(site.id),
    )
    return max(0, HOURLY_CAP - recent)


async def event_rows(
    site,
    settings: SiteNotificationSettings,
    *,
    event: str,
    kind: str,
    lead_id: str | None,
    title: str,
    body: str,
) -> list[dict[str, Any]]:
    """One ``whatsapp_owner`` row per listed number, within the hourly cap. A
    lead event carries only the lead id (the text is built at send time); an
    event without a lead carries its title and body."""
    listed = numbers(settings)
    if not listed:
        return []
    room = await _room(site)
    if room < len(listed):
        logger.warning(
            "owner WhatsApp: site=%s event=%s hit the hourly cap of %d; %d of %d numbers skipped",
            site.id,
            event,
            HOURLY_CAP,
            len(listed) - room,
            len(listed),
        )
        listed = listed[:room]
    payload: dict[str, Any] = {"event": event, "site_ref": str(site.id)}
    if lead_id:
        payload["lead_id"] = lead_id
    else:
        payload.update({"title": title, "body": body, "site_name": site.name or ""})
    return [
        {
            "workspace": site.workspace,
            "kind": kind,
            "sink": SINK,
            "target": number,
            "payload": dict(payload),
        }
        for number in listed
    ]


async def number_allowed(workspace_id: str, site_ref: str, e164: str) -> bool:
    """Send-time check: the number is still on the site's list."""
    site = await ns.find_site(workspace_id, site_ref)
    if site is None:
        return False
    return e164 in numbers(await ns.settings_for(site))


# ---------------------------------------------------------------------------
# Owner-facing operations (the routes)
# ---------------------------------------------------------------------------


async def add_number(
    workspace_id: str, site_id: str, e164: str, *, consent: bool, added_by: str = ""
) -> dict[str, Any]:
    site = await ns._load(workspace_id, site_id)
    await add(site, e164, consent=consent, added_by=added_by)
    return await ns._wire(site)


async def remove_number(workspace_id: str, site_id: str, e164: str) -> dict[str, Any]:
    site = await ns._load(workspace_id, site_id)
    await remove(site, e164)
    return await ns._wire(site)


async def send_test(workspace_id: str, site_id: str) -> dict[str, Any]:
    """Queue one test message to every listed number (counts toward the cap)."""
    from pocketpaw_ee.cloud.notifications import outbox, whatsapp_notify

    site = await ns._load(workspace_id, site_id)
    listed = numbers(await ns.settings_for(site))
    if not listed:
        raise ValidationError(
            "lead_notifications.whatsapp_no_numbers", "Add a WhatsApp number first."
        )
    if await _room(site) < len(listed):
        raise RateLimited(
            "lead_notifications.whatsapp_rate_limited",
            f"This site sent {HOURLY_CAP} WhatsApp messages in the last hour. Try again later.",
        )
    await outbox.enqueue_many(
        [
            {
                "workspace": site.workspace,
                "kind": TEST_KIND,
                "sink": SINK,
                "target": number,
                "payload": {"event": "test", "site_ref": str(site.id), "site_name": site.name},
            }
            for number in listed
        ]
    )
    return {"queued": len(listed), "numbers": listed, "mode": whatsapp_notify.mode()}


__all__ = [
    "HOURLY_CAP",
    "SINK",
    "TEST_KIND",
    "add",
    "add_number",
    "event_rows",
    "normalize",
    "number_allowed",
    "numbers",
    "remove",
    "remove_number",
    "send_test",
    "wire",
]
