# ee/pocketpaw_ee/cloud/leads/notification_settings.py
# Per-site owner notifications: the ONLY writer of ``site_notification_settings``
# (``models/lead_notifications.py``), and the router that turns a site event (a
# captured lead, a concierge handoff, later a booking) into bell/push rows,
# emails and webhook deliveries.
#
# Settings: who gets email (the workspace owner's account address, once it is
# verified on the account OR confirmed through our link, plus up to 5 confirmed
# extras), an optional signed site webhook, and per-event sinks
# (email | webhook | push). No row means the owner's address with email + push
# for every event. Every write is a targeted ``$set`` / ``$inc`` / ``$push`` /
# ``$pull`` / positional update, never a read-modify-write of the whole row.
#
# Confirm flow: adding an address stores it unconfirmed with a fresh nonce and
# queues ONE confirm email carrying a Fernet token (site, workspace, email,
# nonce) that expires after 7 days. Re-sends are limited to one per address per
# 30 minutes (an atomic marker that survives remove/re-add) and 50 per
# workspace per day. An unverified owner address gets the same link. The public confirm page shows a
# button only (GET has no side effect, so link scanners can't confirm); the
# POST confirms. A new nonce voids older links. Unconfirmed, bounced or
# unverified addresses get no other mail, checked at enqueue and at send.
#
# Routing (``dispatch_site_event``): "push" creates the bell rows (and so the
# OS push); "email" queues one email per allowed recipient; "webhook" queues one
# signed delivery to the site webhook. The workspace config is the fallback: its
# Slack always gets the event (subject to its own routes) and its webhook gets
# it when the site has no webhook of its own, with the deprecated flat
# notification fields added so existing ``kind`` filters keep working.

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import NotFound, RateLimited, ValidationError
from pocketpaw_ee.cloud.models.lead_notifications import (
    DEFAULT_EVENT_SINKS,
    LEAD_EVENTS,
    MAX_EXTRA_RECIPIENTS,
    LeadNotificationRecipient,
    SiteNotificationSettings,
    default_events,
)
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc

logger = logging.getLogger(__name__)

CONFIRM_TTL_SECONDS = 7 * 24 * 3600
CONFIRM_RESEND_INTERVAL = timedelta(minutes=30)
CONFIRM_DAILY_CAP = 50
WEBHOOK_DISABLE_THRESHOLD = 10
CONFIRM_KIND = "lead_notifications_confirm"
_VALID_SINKS = frozenset({"email", "webhook", "push"})

# Webhook ``type`` per site event.
EVENT_TYPES = {
    "lead_captured": "lead.captured",
    "handoff": "concierge.handoff",
    "booking": "booking.created",
}


def _now() -> datetime:
    return datetime.now(UTC)


def _coll():
    return SiteNotificationSettings.get_pymongo_collection()


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


def _key(site: _SiteDoc) -> dict[str, str]:
    return {"workspace": site.workspace, "site_id": str(site.id)}


async def settings_for(site: _SiteDoc | None) -> SiteNotificationSettings:
    """The site's settings row, or the unsaved default when it has none."""
    if site is not None:
        doc = await SiteNotificationSettings.find_one(_key(site))
        if doc is not None:
            return doc
    return SiteNotificationSettings(
        workspace=getattr(site, "workspace", "") or "", site_id=str(getattr(site, "id", ""))
    )


async def _ensure_row(site: _SiteDoc) -> None:
    """Create the settings row with defaults if it doesn't exist (atomic upsert)."""
    await _coll().update_one(
        _key(site),
        {
            "$setOnInsert": {
                "include_owner": True,
                "emails": [],
                "webhook_url": None,
                "webhook_secret_enc": "",
                "webhook_secret_prev_enc": "",
                "webhook_secret_rotated_at": None,
                "webhook_failure_count": 0,
                "webhook_disabled_at": None,
                "events": default_events(),
            }
        },
        upsert=True,
    )


async def owner_identity(workspace_id: str) -> tuple[str, bool]:
    """(the workspace owner's account email, whether that address is verified),
    or ("", False) when it can't be resolved."""
    try:
        from pocketpaw_ee.cloud.models.user import User
        from pocketpaw_ee.cloud.models.workspace import Workspace

        ws = await Workspace.get(PydanticObjectId(workspace_id))
        if ws is None or not ws.owner:
            return "", False
        user = await User.get(PydanticObjectId(ws.owner))
        if user is None:
            return "", False
        return str(getattr(user, "email", "") or ""), bool(getattr(user, "is_verified", False))
    except Exception:  # noqa: BLE001 — "no owner email" is a normal answer here
        logger.debug("owner email lookup failed for %s", workspace_id, exc_info=True)
        return "", False


async def owner_email(workspace_id: str) -> str:
    """The owner's account email when it is verified, else ""."""
    email, verified = await owner_identity(workspace_id)
    return email if verified else ""


def _normalize_email(value: str) -> str:
    from pocketpaw.sites_capture.contact_form import looks_like_email

    email = (value or "").strip().lower()
    if len(email) > 320 or not looks_like_email(email) or any(c in email for c in "\r\n<>,;"):
        raise ValidationError("lead_notifications.invalid_email", "That is not a valid email.")
    return email


def _recipient_state(r: LeadNotificationRecipient) -> str:
    if r.bounced_at is not None:
        return "bounced"
    return "confirmed" if r.confirmed_at is not None else "pending"


def _owner_confirmed(settings: SiteNotificationSettings, owner: str) -> bool:
    oc = settings.owner_confirm
    return bool(
        owner
        and oc is not None
        and oc.email == owner.lower()
        and oc.confirmed_at is not None
        and oc.bounced_at is None
    )


def _owner_status(settings: SiteNotificationSettings, owner: str, verified: bool) -> str | None:
    if not owner:
        return None
    if verified:
        return "verified"
    return "confirmed" if _owner_confirmed(settings, owner) else "pending_confirm"


def _site_webhook_active(settings: SiteNotificationSettings) -> bool:
    return bool(
        settings.webhook_url
        and settings.webhook_secret_enc
        and settings.webhook_disabled_at is None
    )


async def _wire(site: _SiteDoc, *, webhook_secret: str | None = None) -> dict[str, Any]:
    from pocketpaw_ee.cloud.notifications import email as email_mod

    settings = await settings_for(site)
    owner, verified = await owner_identity(site.workspace)
    return {
        "site_id": str(site.id),
        "configured": settings.id is not None,
        "include_owner": settings.include_owner,
        "owner_email": owner or None,
        # verified (account) | confirmed (clicked our link) | pending_confirm
        "owner_email_status": _owner_status(settings, owner, verified),
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
    return await _wire(await _load(workspace_id, site_id))


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
    """Patch the settings. A webhook URL is SSRF-checked (DNS included); saving
    one (new or the same) re-arms a webhook that was switched off, and a new URL
    (or one without a secret) mints a signing secret returned in this response
    only. ``clear_webhook`` removes it."""
    from pocketpaw_ee.cloud.audit.webhooks import mint_secret
    from pocketpaw_ee.cloud.auth.sso import crypto
    from pocketpaw_ee.cloud.notifications.delivery import validate_webhook_url

    site = await _load(workspace_id, site_id)
    update: dict[str, Any] = {}
    new_secret: str | None = None
    if include_owner is not None:
        update["include_owner"] = include_owner
    if events is not None:
        for event, sinks in _clean_events(events).items():
            update[f"events.{event}"] = sinks
    if clear_webhook:
        update.update(
            webhook_url=None,
            webhook_secret_enc="",
            webhook_secret_prev_enc="",
            webhook_failure_count=0,
            webhook_disabled_at=None,
        )
    elif webhook_url is not None and webhook_url.strip():
        url = webhook_url.strip()
        await validate_webhook_url(url)
        current = await settings_for(site)
        if url != current.webhook_url or not current.webhook_secret_enc:
            new_secret = mint_secret()
            update["webhook_secret_enc"] = crypto.encrypt(new_secret)
            update["webhook_secret_prev_enc"] = ""
        update.update(webhook_url=url, webhook_failure_count=0, webhook_disabled_at=None)
    await _ensure_row(site)
    if update:
        await _coll().update_one(_key(site), {"$set": update})
    return await _wire(site, webhook_secret=new_secret)


async def rotate_webhook_secret(workspace_id: str, site_id: str) -> dict[str, Any]:
    """Mint a new signing secret for the site webhook (returned once) and re-arm
    it. The replaced secret keeps co-signing for the grace window."""
    from pocketpaw_ee.cloud.audit.webhooks import mint_secret
    from pocketpaw_ee.cloud.auth.sso import crypto

    site = await _load(workspace_id, site_id)
    current = await settings_for(site)
    if not current.webhook_url:
        raise NotFound("lead_notification_webhook", site_id)
    secret = mint_secret()
    await _coll().update_one(
        _key(site),
        {
            "$set": {
                "webhook_secret_prev_enc": current.webhook_secret_enc,
                "webhook_secret_enc": crypto.encrypt(secret),
                "webhook_secret_rotated_at": _now(),
                "webhook_failure_count": 0,
                "webhook_disabled_at": None,
            }
        },
    )
    return await _wire(site, webhook_secret=secret)


def _confirm_token(site: _SiteDoc, email: str, nonce: str) -> str:
    from pocketpaw_ee.cloud.auth.sso import crypto

    body = json.dumps({"s": str(site.id), "w": site.workspace, "e": email, "n": nonce})
    return crypto.encrypt(body)


def confirm_url(token: str) -> str:
    from pocketpaw_ee.cloud.notifications import email as email_mod

    return f"{email_mod.api_base_url()}/api/v1/lead-notifications/confirm/{token}"


def _require_public_base_url() -> None:
    """In production a confirm link must point at a real public origin, not the
    localhost default."""
    env = os.environ.get("POCKETPAW_ENV", "").strip().lower()
    if (
        env in ("production", "prod")
        and not os.environ.get("POCKETPAW_PUBLIC_BASE_URL", "").strip()
    ):
        raise ValidationError(
            "lead_notifications.public_url_unset",
            "POCKETPAW_PUBLIC_BASE_URL is not set, so a confirm link can't be built.",
        )


async def _gate_confirm(site: _SiteDoc, address: str) -> None:
    """The confirm-email rate limits, raising ``RateLimited``: 50 per workspace
    per day, and one per (site, address) per 30 minutes. The 30-minute gate is
    an atomic marker that outlives the recipient row, so removing and re-adding
    an address doesn't reset it."""
    from pocketpaw_ee.cloud.notifications import outbox

    now = _now()
    sent_today = await outbox.count_recent(
        workspace=site.workspace, kind=CONFIRM_KIND, since=now - timedelta(days=1)
    )
    if sent_today >= CONFIRM_DAILY_CAP:
        raise RateLimited(
            "lead_notifications.confirm_daily_cap",
            f"This workspace sent {CONFIRM_DAILY_CAP} confirm emails today. Try again tomorrow.",
        )
    marker = f"confirm:{site.workspace}:{site.id}:{address}"
    if not await outbox.claim_marker(marker, CONFIRM_RESEND_INTERVAL, now=now):
        raise RateLimited(
            "lead_notifications.confirm_rate_limited",
            "A confirm email was sent to this address recently. Try again in 30 minutes.",
        )


async def _queue_confirm(site: _SiteDoc, address: str, nonce: str) -> None:
    from pocketpaw_ee.cloud.notifications import email as email_mod
    from pocketpaw_ee.cloud.notifications import outbox

    await outbox.enqueue(
        workspace=site.workspace,
        kind=CONFIRM_KIND,
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


async def _send_owner_confirm(site: _SiteDoc, owner: str) -> None:
    """Send the owner's (unverified) account address the confirm link. Raises
    ``RateLimited`` like any other confirm."""
    address = owner.lower()
    await _gate_confirm(site, address)
    nonce = secrets.token_urlsafe(12)
    now = _now()
    record = LeadNotificationRecipient(
        email=address, added_at=now, added_by="owner", confirm_nonce=nonce, confirm_sent_at=now
    )
    await _ensure_row(site)
    await _coll().update_one(
        _key(site), {"$set": {"owner_confirm": record.model_dump(mode="python")}}
    )
    await _queue_confirm(site, address, nonce)


async def ensure_owner_confirm_sent(site: _SiteDoc) -> None:
    """Best effort: when the owner's address needs a confirm, send it, at most
    once a day per site. Silent on rate limits; never raises."""
    try:
        owner, verified = await owner_identity(site.workspace)
        if not owner or verified:
            return
        settings = await settings_for(site)
        if not settings.include_owner or _owner_confirmed(settings, owner):
            return
        from pocketpaw_ee.cloud.notifications import outbox

        # Unprompted sends (triggered by a lead) at most once a day per site,
        # so an owner who ignores the link isn't mailed on every lead.
        if not await outbox.claim_marker(
            f"owner_confirm_auto:{site.workspace}:{site.id}", timedelta(days=1)
        ):
            return
        await _send_owner_confirm(site, owner)
    except RateLimited:
        return
    except Exception:
        logger.warning("could not send the owner confirm for site %s", site.id, exc_info=True)


async def add_recipient(
    workspace_id: str, site_id: str, email: str, *, added_by: str = ""
) -> dict[str, Any]:
    """Add an unconfirmed address and queue its confirm email. Re-adding an
    address that is pending or bounced re-sends a fresh link; a confirmed one
    is left as is. The owner's own address, when the account hasn't verified
    it, re-sends the owner confirm instead. Rate limits: see ``_gate_confirm``."""
    from pocketpaw_ee.cloud.notifications import email as email_mod

    address = _normalize_email(email)
    site = await _load(workspace_id, site_id)
    if not email_mod.is_configured():
        raise ValidationError(
            "lead_notifications.email_disabled",
            "Email is not set up on this server, so the address can't be confirmed.",
        )
    _require_public_base_url()
    current = await settings_for(site)
    owner, verified = await owner_identity(workspace_id)
    if owner and address == owner.lower():
        if not verified and not _owner_confirmed(current, owner):
            await _send_owner_confirm(site, owner)
        return await _wire(site)

    existing = next((r for r in current.emails if r.email == address), None)
    if existing is not None and existing.confirmed_at is not None and existing.bounced_at is None:
        return await _wire(site)
    if existing is None and len(current.emails) >= MAX_EXTRA_RECIPIENTS:
        raise ValidationError(
            "lead_notifications.too_many_recipients",
            f"A site can notify at most {MAX_EXTRA_RECIPIENTS} extra addresses.",
        )
    await _gate_confirm(site, address)

    nonce = secrets.token_urlsafe(12)
    now = _now()
    await _ensure_row(site)
    if existing is not None:
        result = await _coll().update_one(
            {**_key(site), "emails.email": address},
            {
                "$set": {
                    "emails.$.confirm_nonce": nonce,
                    "emails.$.confirm_sent_at": now,
                    "emails.$.confirmed_at": None,
                    "emails.$.bounced_at": None,
                }
            },
        )
    else:
        recipient = LeadNotificationRecipient(
            email=address,
            added_at=now,
            added_by=added_by,
            confirm_nonce=nonce,
            confirm_sent_at=now,
        )
        # The filter IS the cap and the dedupe, so two concurrent adds can't
        # overshoot 5 or insert the same address twice.
        result = await _coll().update_one(
            {
                **_key(site),
                "emails.email": {"$ne": address},
                f"emails.{MAX_EXTRA_RECIPIENTS - 1}": {"$exists": False},
            },
            {"$push": {"emails": recipient.model_dump(mode="python")}},
        )
    if getattr(result, "modified_count", 0) == 0:
        raise ValidationError(
            "lead_notifications.too_many_recipients",
            f"A site can notify at most {MAX_EXTRA_RECIPIENTS} extra addresses.",
        )
    await _queue_confirm(site, address, nonce)
    return await _wire(site)


async def remove_recipient(workspace_id: str, site_id: str, email: str) -> dict[str, Any]:
    site = await _load(workspace_id, site_id)
    address = email.strip().lower()
    result = await _coll().update_one(_key(site), {"$pull": {"emails": {"email": address}}})
    if getattr(result, "modified_count", 0) == 0:
        raise NotFound("lead_notification_recipient", email)
    return await _wire(site)


async def _token_target(token: str) -> tuple[_SiteDoc, str, str, bool] | None:
    """(site, email, nonce, is_owner) named by a live confirm token, or None."""
    from cryptography.fernet import InvalidToken

    from pocketpaw_ee.cloud.auth.sso import crypto

    try:
        claims = json.loads(crypto.decrypt_with_ttl(token, CONFIRM_TTL_SECONDS))
        site_id, workspace_id = str(claims["s"]), str(claims["w"])
        address, nonce = str(claims["e"]).lower(), str(claims["n"])
    except (InvalidToken, ValueError, KeyError, TypeError):
        return None
    site = await find_site(workspace_id, site_id)
    if site is None:
        return None
    settings = await settings_for(site)
    oc = settings.owner_confirm
    if oc is not None and oc.email == address and secrets.compare_digest(oc.confirm_nonce, nonce):
        return site, address, nonce, True
    match = next((r for r in settings.emails if r.email == address), None)
    if match is None or not secrets.compare_digest(match.confirm_nonce, nonce):
        return None
    return site, address, nonce, False


async def check_token(token: str) -> tuple[str, str]:
    """No side effects: ``("valid", site_name)`` or ``("invalid", "")``. Backs
    the GET confirm page, which only renders a button."""
    target = await _token_target(token)
    return ("valid", target[0].name or "") if target is not None else ("invalid", "")


async def confirm(token: str) -> tuple[str, str]:
    """Confirm an address from its emailed token. Returns ``(state, site_name)``
    with state ``confirmed`` (also on a repeat) or ``invalid`` (bad, expired,
    superseded, or the address/site is gone)."""
    target = await _token_target(token)
    if target is None:
        return "invalid", ""
    site, address, nonce, is_owner = target
    if is_owner:
        await _coll().update_one(
            {
                **_key(site),
                "owner_confirm.email": address,
                "owner_confirm.confirm_nonce": nonce,
                "owner_confirm.confirmed_at": None,
            },
            {"$set": {"owner_confirm.confirmed_at": _now()}},
        )
        return "confirmed", site.name or ""
    await _coll().update_one(
        {
            **_key(site),
            "emails": {
                "$elemMatch": {"email": address, "confirm_nonce": nonce, "confirmed_at": None}
            },
        },
        {"$set": {"emails.$.confirmed_at": _now()}},
    )
    return "confirmed", site.name or ""


async def send_test(workspace_id: str, site_id: str) -> dict[str, Any]:
    """Queue a test email to every allowed recipient and a test delivery to the
    site webhook. Returns what was queued."""
    from pocketpaw_ee.cloud.notifications import email as email_mod
    from pocketpaw_ee.cloud.notifications import outbox
    from pocketpaw_ee.cloud.notifications.delivery import new_event_envelope

    site = await _load(workspace_id, site_id)
    settings = await settings_for(site)
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


async def allowed_recipients(workspace_id: str, site: _SiteDoc) -> list[str]:
    """Addresses that may get this site's mail now: the owner's (when included,
    and either verified on the account or confirmed through our link) plus
    confirmed, unbounced extras. Deduped, case-insensitively."""
    settings = await settings_for(site)
    out: list[str] = []
    if settings.include_owner:
        owner, verified = await owner_identity(workspace_id)
        if owner and (verified or _owner_confirmed(settings, owner)):
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
    result = await _coll().update_one(
        {
            "workspace": workspace_id,
            "site_id": site_id,
            "emails": {"$elemMatch": {"email": email.lower(), "bounced_at": None}},
        },
        {"$set": {"emails.$.bounced_at": _now()}},
    )
    if getattr(result, "modified_count", 0) == 0:
        result = await _coll().update_one(
            {
                "workspace": workspace_id,
                "site_id": site_id,
                "owner_confirm.email": email.lower(),
                "owner_confirm.bounced_at": None,
            },
            {"$set": {"owner_confirm.bounced_at": _now()}},
        )
    if getattr(result, "modified_count", 0) == 0:
        logger.info("permanent bounce for a non-listed address on site %s", site_id)


async def webhook_target(workspace_id: str, site_id: str) -> tuple[str, list[str]] | None:
    """(url, signing secrets) of the site webhook while it is active, else None.
    Site webhooks are always signed."""
    from pocketpaw_ee.cloud.notifications.service import signing_secrets

    site = await find_site(workspace_id, site_id)
    if site is None:
        return None
    settings = await settings_for(site)
    if not _site_webhook_active(settings):
        return None
    return str(settings.webhook_url), signing_secrets(
        settings.webhook_secret_enc,
        settings.webhook_secret_prev_enc,
        settings.webhook_secret_rotated_at,
    )


async def record_webhook_result(workspace_id: str, site_id: str, *, ok: bool) -> None:
    """Atomic: reset on success; on a dead delivery ``$inc`` the counter and,
    in a separate conditional ``$set``, switch the webhook off at the threshold."""
    key = {"workspace": workspace_id, "site_id": site_id}
    if ok:
        await _coll().update_one(
            {**key, "webhook_failure_count": {"$gt": 0}}, {"$set": {"webhook_failure_count": 0}}
        )
        return
    await _coll().update_one(key, {"$inc": {"webhook_failure_count": 1}})
    await _coll().update_one(
        {
            **key,
            "webhook_failure_count": {"$gte": WEBHOOK_DISABLE_THRESHOLD},
            "webhook_disabled_at": None,
        },
        {"$set": {"webhook_disabled_at": _now()}},
    )


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
    email is a short notification. Never raises; returns per-sink counts. A
    failed site or settings lookup degrades to the defaults: the bell still rings."""
    from pocketpaw_ee.cloud.notifications import delivery, outbox
    from pocketpaw_ee.cloud.notifications import email as email_mod
    from pocketpaw_ee.cloud.notifications import service as notifications_service

    counts = {"push": 0, "email": 0, "webhook": 0}
    site: _SiteDoc | None = None
    settings = SiteNotificationSettings(workspace=workspace_id, site_id="")
    try:
        site = await find_site(workspace_id, site_ref)
        settings = await settings_for(site)
    except Exception:
        logger.warning("site lookup failed for %s; using default routing", site_ref, exc_info=True)
        site = None
    sinks = set(settings.events.get(event, DEFAULT_EVENT_SINKS))

    if "push" in sinks and push_recipients:
        try:
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
        except Exception:
            logger.warning("bell/push for %s failed", kind, exc_info=True)

    try:
        fields: dict[str, Any] = (
            {"lead_id": lead_id} if lead_id else {"data": dict(event_data or {})}
        )
        envelope = delivery.new_event_envelope(EVENT_TYPES.get(event, event), **fields)
        rows: list[dict[str, Any]] = []
        if site is not None and "email" in sinks and email_mod.is_configured():
            # An owner address the account hasn't verified gets a confirm link
            # (rate-limited) instead of this mail; once confirmed it gets mail.
            await ensure_owner_confirm_sent(site)
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
        # has none of its own. That webhook predates the envelope and its
        # consumers filter on the flat ``kind``, so those fields ride along.
        workspace_event = {
            **envelope,
            "legacy": {
                "workspace_id": workspace_id,
                "recipient_id": None,
                "actor_id": None,
                "kind": kind,
                "title": title,
                "body": body,
            },
        }
        await delivery.enqueue_workspace_event(
            workspace_id=workspace_id,
            kind=kind,
            slack_text=f"{title}\n{body}" if body else title,
            webhook_event=workspace_event,
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
    "check_token",
    "confirm",
    "confirm_url",
    "dispatch_site_event",
    "ensure_owner_confirm_sent",
    "find_site",
    "get_settings",
    "owner_email",
    "owner_identity",
    "record_bounce",
    "record_webhook_result",
    "recipient_allowed",
    "remove_recipient",
    "rotate_webhook_secret",
    "send_test",
    "settings_for",
    "update_settings",
    "webhook_target",
]
