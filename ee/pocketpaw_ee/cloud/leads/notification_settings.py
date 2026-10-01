# ee/pocketpaw_ee/cloud/leads/notification_settings.py
# Per-site owner notifications: the ONLY writer of ``Site.lead_notifications``,
# and the router that turns a site event (a captured lead, a concierge handoff,
# later a booking) into bell/push rows, emails and webhook deliveries.
#
# Settings (``models/lead_notifications.py``): who gets email (the workspace
# owner's account address plus up to 5 confirmed extras), an optional signed
# site webhook, and per-event sinks (email | webhook | push). Unset means the
# owner's address with email + push for every event.
#
# Confirm flow: adding an address stores it unconfirmed with a fresh nonce and
# queues ONE confirm email carrying a Fernet token (site, workspace, email,
# nonce) that expires after 7 days. The public confirm route checks the token
# and the nonce, so removing and re-adding an address kills older links.
# Confirming twice is a no-op. An unconfirmed or bounced address gets nothing
# else, checked when mail is queued AND again when the outbox sends it.
#
# Routing (``dispatch_site_event``): "push" creates the bell rows (and so the
# OS push, which follows every bell row); "email" queues one email per allowed
# recipient; "webhook" queues one signed delivery to the site webhook. The
# workspace config stays the fallback: its Slack sink always gets the event
# (subject to its own routes), and its webhook gets it when the site has no
# webhook of its own. Every delivery goes through the notification outbox, so
# the visitor's request never waits on mail or HTTP.
#
# Site writes are targeted ``$set`` / ``$inc`` on ``lead_notifications`` only,
# never a whole-document save, so they can't clobber a concurrent Site edit.

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.models.lead_notifications import (
    DEFAULT_EVENT_SINKS,
    LEAD_EVENTS,
    MAX_EXTRA_RECIPIENTS,
    LeadNotificationRecipient,
    LeadNotificationSettings,
)
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

logger = logging.getLogger(__name__)

CONFIRM_TTL_SECONDS = 7 * 24 * 3600
WEBHOOK_DISABLE_THRESHOLD = 10
_VALID_SINKS = frozenset({"email", "webhook", "push"})

# Webhook ``type`` per site event.
EVENT_TYPES = {
    "lead_captured": "lead.captured",
    "handoff": "concierge.handoff",
    "booking": "booking.created",
}


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


async def find_site(workspace_id: str, site_ref: str) -> _SiteDoc | None:
    """A site in ``workspace_id`` by object id or by ``script_name`` (what leads
    and ``lead.captured`` carry). None when absent or in another workspace."""
    if not workspace_id or not site_ref:
        return None
    try:
        oid = PydanticObjectId(site_ref)
    except Exception:  # noqa: BLE001 — not an ObjectId, try the script name
        oid = None
    if oid is not None:
        site = await _SiteDoc.find_one({"_id": oid, "workspace": workspace_id})
        if site is not None:
            return site
    return await _SiteDoc.find_one({"workspace": workspace_id, "script_name": site_ref})


async def _load(workspace_id: str, site_id: str) -> _SiteDoc:
    site = await find_site(workspace_id, site_id)
    if site is None:
        raise NotFound("site", site_id)
    return site


def effective(site: _SiteDoc | None) -> LeadNotificationSettings:
    settings = getattr(site, "lead_notifications", None) if site is not None else None
    return settings if settings is not None else LeadNotificationSettings()


async def owner_email(workspace_id: str) -> str:
    """The workspace owner's account email, or "" when it can't be resolved."""
    try:
        from pocketpaw_ee.cloud.models.user import User
        from pocketpaw_ee.cloud.models.workspace import Workspace

        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if ws is None or not ws.owner:
            return ""
        user = await User.get(PydanticObjectId(ws.owner))
        return str(getattr(user, "email", "") or "") if user is not None else ""
    except Exception:  # noqa: BLE001 — "no owner email" is a normal answer here
        logger.debug("owner email lookup failed for %s", workspace_id, exc_info=True)
        return ""


async def _write(site: _SiteDoc, settings: LeadNotificationSettings) -> None:
    coll = _SiteDoc.get_pymongo_collection()
    await coll.update_one(
        {"_id": site.id, "workspace": site.workspace},
        {"$set": {"lead_notifications": settings.model_dump(mode="python")}},
    )
    site.lead_notifications = settings


def _normalize_email(value: str) -> str:
    from pocketpaw.sites_capture.contact_form import looks_like_email

    email = (value or "").strip()
    if len(email) > 320 or not looks_like_email(email) or any(c in email for c in "\r\n<>,;"):
        raise ValidationError("lead_notifications.invalid_email", "That is not a valid email.")
    return email


def _recipient_state(r: LeadNotificationRecipient) -> str:
    if r.bounced_at is not None:
        return "bounced"
    return "confirmed" if r.confirmed_at is not None else "pending"


def to_wire(
    site: _SiteDoc,
    settings: LeadNotificationSettings,
    owner: str,
    *,
    webhook_secret: str | None = None,
) -> dict[str, Any]:
    from pocketpaw_ee.cloud.notifications import email as email_mod

    return {
        "site_id": str(site.id),
        "configured": getattr(site, "lead_notifications", None) is not None,
        "include_owner": settings.include_owner,
        "owner_email": owner or None,
        "emails": [
            {
                "email": r.email,
                "status": _recipient_state(r),
                "added_at": r.added_at,
                "confirmed_at": r.confirmed_at,
            }
            for r in settings.emails
        ],
        "webhook_url": settings.webhook_url,
        "has_webhook_secret": bool(settings.webhook_secret_enc),
        "webhook_secret": webhook_secret,
        "webhook_disabled_at": settings.webhook_disabled_at,
        "webhook_failure_count": settings.webhook_failure_count,
        "events": {e: list(settings.events.get(e, DEFAULT_EVENT_SINKS)) for e in LEAD_EVENTS},
        "email_enabled": email_mod.is_configured(),
    }


# ---------------------------------------------------------------------------
# Owner-facing operations (the routes)
# ---------------------------------------------------------------------------


async def get_settings(workspace_id: str, site_id: str) -> dict[str, Any]:
    site = await _load(workspace_id, site_id)
    return to_wire(site, effective(site), await owner_email(workspace_id))


def _clean_events(events: dict[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for event, sinks in events.items():
        if event not in LEAD_EVENTS:
            raise ValidationError("lead_notifications.unknown_event", f"Unknown event '{event}'.")
        bad = [s for s in sinks if s not in _VALID_SINKS]
        if bad:
            raise ValidationError("lead_notifications.unknown_sink", f"Unknown sink '{bad[0]}'.")
        out[event] = list(dict.fromkeys(sinks))
    return out


async def update_settings(
    workspace_id: str,
    site_id: str,
    *,
    include_owner: bool | None = None,
    events: dict[str, list[str]] | None = None,
    webhook_url: str | None = None,
    clear_webhook: bool = False,
) -> dict[str, Any]:
    """Patch the settings. A new webhook URL is SSRF-checked (DNS included),
    mints a signing secret returned in this response only, and re-arms a
    webhook that was switched off. ``clear_webhook`` removes it."""
    from pocketpaw_ee.cloud.audit.webhooks import mint_secret
    from pocketpaw_ee.cloud.auth.sso import crypto
    from pocketpaw_ee.cloud.notifications.delivery import validate_webhook_url

    site = await _load(workspace_id, site_id)
    settings = effective(site).model_copy(deep=True)
    new_secret: str | None = None
    if include_owner is not None:
        settings.include_owner = include_owner
    if events is not None:
        settings.events = {**settings.events, **_clean_events(events)}
    if clear_webhook:
        settings.webhook_url = None
        settings.webhook_secret_enc = ""
        settings.webhook_failure_count = 0
        settings.webhook_disabled_at = None
    elif webhook_url is not None and webhook_url.strip():
        url = webhook_url.strip()
        await validate_webhook_url(url)
        if url != settings.webhook_url or not settings.webhook_secret_enc:
            new_secret = mint_secret()
            settings.webhook_secret_enc = crypto.encrypt(new_secret)
            settings.webhook_failure_count = 0
            settings.webhook_disabled_at = None
        settings.webhook_url = url
    await _write(site, settings)
    return to_wire(site, settings, await owner_email(workspace_id), webhook_secret=new_secret)


def _confirm_token(site: _SiteDoc, email: str, nonce: str) -> str:
    from pocketpaw_ee.cloud.auth.sso import crypto

    body = json.dumps({"s": str(site.id), "w": site.workspace, "e": email, "n": nonce})
    return crypto.encrypt(body)


def confirm_url(token: str) -> str:
    from pocketpaw_ee.cloud.notifications import email as email_mod

    return f"{email_mod.api_base_url()}/api/v1/lead-notifications/confirm/{token}"


async def add_recipient(
    workspace_id: str, site_id: str, email: str, *, added_by: str = ""
) -> dict[str, Any]:
    """Add an unconfirmed address and queue its confirm email. Re-adding an
    address that is pending or bounced re-sends a fresh link; a confirmed one
    is left as is."""
    from pocketpaw_ee.cloud.notifications import email as email_mod
    from pocketpaw_ee.cloud.notifications import outbox

    address = _normalize_email(email)
    site = await _load(workspace_id, site_id)
    settings = effective(site).model_copy(deep=True)
    existing = next((r for r in settings.emails if r.email.lower() == address.lower()), None)
    owner = await owner_email(workspace_id)
    if existing is not None and existing.confirmed_at is not None and existing.bounced_at is None:
        return to_wire(site, settings, owner)
    if existing is None and len(settings.emails) >= MAX_EXTRA_RECIPIENTS:
        raise ValidationError(
            "lead_notifications.too_many_recipients",
            f"A site can notify at most {MAX_EXTRA_RECIPIENTS} extra addresses.",
        )
    if not email_mod.is_configured():
        raise ValidationError(
            "lead_notifications.email_disabled",
            "Email is not set up on this server, so the address can't be confirmed.",
        )
    nonce = secrets.token_urlsafe(12)
    if existing is None:
        existing = LeadNotificationRecipient(email=address, added_at=_now(), added_by=added_by)
        settings.emails.append(existing)
    existing.confirm_nonce = nonce
    existing.confirmed_at = None
    existing.bounced_at = None
    await _write(site, settings)
    await outbox.enqueue(
        workspace=workspace_id,
        kind="lead_notifications_confirm",
        sink="email",
        target=address,
        payload={
            "template": "confirm",
            "site_ref": str(site.id),
            "site_name": site.name,
            "confirm_url": confirm_url(_confirm_token(site, address, nonce)),
            "footer_url": email_mod.site_settings_url(str(site.id)),
        },
    )
    return to_wire(site, settings, owner)


async def remove_recipient(workspace_id: str, site_id: str, email: str) -> dict[str, Any]:
    site = await _load(workspace_id, site_id)
    settings = effective(site).model_copy(deep=True)
    before = len(settings.emails)
    settings.emails = [r for r in settings.emails if r.email.lower() != email.strip().lower()]
    if len(settings.emails) == before:
        raise NotFound("lead_notification_recipient", email)
    await _write(site, settings)
    return to_wire(site, settings, await owner_email(workspace_id))


async def confirm(token: str) -> tuple[str, str]:
    """Confirm an address from its emailed token. Returns ``(state, site_name)``
    with state ``confirmed`` (also on a repeat click) or ``invalid`` (bad,
    expired, superseded, or the address/site is gone)."""
    from cryptography.fernet import InvalidToken

    from pocketpaw_ee.cloud.auth.sso import crypto

    try:
        claims = json.loads(crypto.decrypt_with_ttl(token, CONFIRM_TTL_SECONDS))
        site_id, workspace_id, address, nonce = claims["s"], claims["w"], claims["e"], claims["n"]
    except (InvalidToken, ValueError, KeyError, TypeError):
        return "invalid", ""
    site = await find_site(str(workspace_id), str(site_id))
    if site is None:
        return "invalid", ""
    settings = effective(site).model_copy(deep=True)
    match = next((r for r in settings.emails if r.email.lower() == str(address).lower()), None)
    if match is None or not secrets.compare_digest(match.confirm_nonce, str(nonce)):
        return "invalid", ""
    if match.confirmed_at is None:
        match.confirmed_at = _now()
        await _write(site, settings)
    return "confirmed", site.name or ""


async def send_test(workspace_id: str, site_id: str) -> dict[str, Any]:
    """Queue a test email to every allowed recipient and a test delivery to the
    site webhook. Returns what was queued."""
    from pocketpaw_ee.cloud.notifications import email as email_mod
    from pocketpaw_ee.cloud.notifications import outbox
    from pocketpaw_ee.cloud.notifications.delivery import new_event_envelope

    site = await _load(workspace_id, site_id)
    settings = effective(site)
    rows: list[dict[str, Any]] = []
    emails: list[str] = []
    if email_mod.is_configured():
        emails = await allowed_recipients(workspace_id, site)
        for address in emails:
            rows.append(
                {
                    "workspace": workspace_id,
                    "kind": "lead_notifications_test",
                    "sink": "email",
                    "target": address,
                    "payload": {
                        "template": "test",
                        "site_ref": str(site.id),
                        "site_name": site.name,
                        "footer_url": email_mod.site_settings_url(str(site.id)),
                    },
                }
            )
    webhook = _site_webhook_active(settings)
    if webhook:
        rows.append(
            {
                "workspace": workspace_id,
                "kind": "lead_notifications_test",
                "sink": "webhook",
                "target": settings.webhook_url,
                "payload": new_event_envelope(
                    "notification.test", data={"site_id": str(site.id), "site_name": site.name}
                ),
                "webhook_ref": f"site:{site.id}",
            }
        )
    await outbox.enqueue_many(rows)
    return {"emails": emails, "webhook": webhook}


# ---------------------------------------------------------------------------
# Outbox hooks (send-time checks)
# ---------------------------------------------------------------------------


def _site_webhook_active(settings: LeadNotificationSettings) -> bool:
    return bool(
        settings.webhook_url
        and settings.webhook_secret_enc
        and settings.webhook_disabled_at is None
    )


async def allowed_recipients(workspace_id: str, site: _SiteDoc) -> list[str]:
    """Addresses that may get this site's mail now: the owner's (when included)
    plus confirmed, unbounced extras. Deduped, case-insensitively."""
    settings = effective(site)
    out: list[str] = []
    if settings.include_owner:
        owner = await owner_email(workspace_id)
        if owner:
            out.append(owner)
    out += [r.email for r in settings.emails if r.confirmed_at and r.bounced_at is None]
    seen: set[str] = set()
    return [a for a in out if not (a.lower() in seen or seen.add(a.lower()))]


async def recipient_allowed(workspace_id: str, site_id: str, email: str) -> bool:
    site = await find_site(workspace_id, site_id)
    if site is None:
        return False
    return email.lower() in {a.lower() for a in await allowed_recipients(workspace_id, site)}


async def record_bounce(workspace_id: str, site_id: str, email: str) -> None:
    site = await find_site(workspace_id, site_id)
    if site is None or site.lead_notifications is None:
        return
    settings = site.lead_notifications.model_copy(deep=True)
    hit = False
    for r in settings.emails:
        if r.email.lower() == email.lower() and r.bounced_at is None:
            r.bounced_at = _now()
            hit = True
    if hit:
        await _write(site, settings)
    else:
        logger.info("permanent bounce for a non-listed address on site %s", site.id)


async def webhook_target(workspace_id: str, site_id: str) -> tuple[str, str] | None:
    from pocketpaw_ee.cloud.auth.sso import crypto

    site = await find_site(workspace_id, site_id)
    settings = effective(site)
    if site is None or not _site_webhook_active(settings):
        return None
    return str(settings.webhook_url), crypto.decrypt(settings.webhook_secret_enc)


async def record_webhook_result(workspace_id: str, site_id: str, *, ok: bool) -> None:
    site = await find_site(workspace_id, site_id)
    if site is None or site.lead_notifications is None:
        return
    settings = site.lead_notifications.model_copy(deep=True)
    if ok:
        if settings.webhook_failure_count == 0:
            return
        settings.webhook_failure_count = 0
    else:
        settings.webhook_failure_count += 1
        if (
            settings.webhook_failure_count >= WEBHOOK_DISABLE_THRESHOLD
            and settings.webhook_disabled_at is None
        ):
            settings.webhook_disabled_at = _now()
    await _write(site, settings)


# ---------------------------------------------------------------------------
# Event routing
# ---------------------------------------------------------------------------


async def dispatch_site_event(
    *,
    workspace_id: str,
    site_ref: str,
    event: str,
    kind: str,
    title: str,
    body: str,
    source: Any,
    push_recipients: list[str],
    lead_id: str | None = None,
    event_data: dict[str, Any] | None = None,
    link: str = "",
) -> dict[str, int]:
    """Route one site event to its sinks; see the module header. ``lead_id``
    makes the email the full lead email and the webhook data the lead (both
    loaded at send time); otherwise ``event_data`` is the webhook data and the
    email is a short notification. Never raises; returns per-sink counts."""
    from pocketpaw_ee.cloud.notifications import delivery, outbox
    from pocketpaw_ee.cloud.notifications import email as email_mod
    from pocketpaw_ee.cloud.notifications import service as notifications_service

    counts = {"push": 0, "email": 0, "webhook": 0}
    try:
        site = await find_site(workspace_id, site_ref)
        settings = effective(site)
        sinks = set(settings.events.get(event, DEFAULT_EVENT_SINKS))
        envelope_fields: dict[str, Any] = (
            {"lead_id": lead_id} if lead_id else {"data": dict(event_data or {})}
        )
        envelope = delivery.new_event_envelope(EVENT_TYPES.get(event, event), **envelope_fields)

        if "push" in sinks and push_recipients:
            created = await notifications_service.create_many(
                workspace_id=workspace_id,
                recipients=push_recipients,
                kind=kind,
                title=title,
                body=body,
                source=source,
                deliver_external=False,
            )
            counts["push"] = len(created)

        rows: list[dict[str, Any]] = []
        if site is not None and "email" in sinks and email_mod.is_configured():
            site_id = str(site.id)
            if lead_id:
                payload: dict[str, Any] = {"template": "lead", "lead_id": lead_id}
            else:
                payload = {
                    "template": "notification",
                    "title": title,
                    "body": body,
                    "site_name": site.name,
                    "link": link or email_mod.lead_url(site_id),
                    "footer_url": email_mod.site_settings_url(site_id),
                }
            payload["site_ref"] = site_id
            for address in await allowed_recipients(workspace_id, site):
                rows.append(
                    {
                        "workspace": workspace_id,
                        "kind": kind,
                        "sink": "email",
                        "target": address,
                        "payload": dict(payload),
                    }
                )
        site_webhook = site is not None and _site_webhook_active(settings)
        if site_webhook and "webhook" in sinks:
            rows.append(
                {
                    "workspace": workspace_id,
                    "kind": kind,
                    "sink": "webhook",
                    "target": settings.webhook_url,
                    "payload": envelope,
                    "webhook_ref": f"site:{site.id}",
                }
            )
        if rows:
            await outbox.enqueue_many(rows)
        counts["email"] = sum(1 for r in rows if r["sink"] == "email")
        counts["webhook"] = sum(1 for r in rows if r["sink"] == "webhook")

        # Workspace fallback: its Slack always, its webhook only when the site
        # has none of its own.
        await delivery.enqueue_workspace_event(
            workspace_id=workspace_id,
            kind=kind,
            slack_text=f"{title}\n{body}" if body else title,
            webhook_event=envelope,
            include_webhook=not site_webhook,
        )
    except Exception:
        logger.warning("site event dispatch failed (event=%s)", event, exc_info=True)
    return counts


__all__ = [
    "CONFIRM_TTL_SECONDS",
    "EVENT_TYPES",
    "add_recipient",
    "allowed_recipients",
    "confirm",
    "confirm_url",
    "dispatch_site_event",
    "effective",
    "find_site",
    "get_settings",
    "owner_email",
    "record_bounce",
    "record_webhook_result",
    "recipient_allowed",
    "remove_recipient",
    "send_test",
    "update_settings",
    "webhook_target",
]
