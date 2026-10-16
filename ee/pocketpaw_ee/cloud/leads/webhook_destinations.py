# ee/pocketpaw_ee/cloud/leads/webhook_destinations.py
# A site's lead-notification webhook DESTINATIONS (``SiteNotificationSettings.
# webhooks``, at most ``MAX_WEBHOOKS``): CRUD, secret rotation, per-destination
# test, send-time target lookup and health, the routing rows a site event turns
# into, and the settings preview. Together with ``notification_settings`` it is
# the only writer of ``site_notification_settings``; it borrows that module's
# site loading, row key and settings wire.
#
# Each destination has a platform (the owner's override, else detected from the
# URL by ``notifications.webhook_platforms``) and a template (title, fields,
# link) that ``notifications.webhook_formats`` renders at send time. Every one
# gets a signing secret, but only json deliveries are signed, so the secret is
# shown once only when the destination is (or becomes) json.
#
# Legacy single webhook: the flat ``webhook_url`` fields read as destination id
# ``legacy`` (``destinations``). Every write here first runs ``migrate_legacy``,
# which moves them into the list in ONE conditional update whose filter pins the
# values it read, so a concurrent counter bump makes it retry instead of being
# lost. Every other write is a targeted ``$push`` / ``$pull`` / positional
# ``webhooks.$`` update; the add filter is the cap and the URL dedupe.
#
# Outbox rows carry ``webhook_ref = "site:<site_id>:<webhook_id>"``; a row queued
# before destinations existed carries "site:<site_id>" and means ``legacy``.
# URLs pass ``validate_webhook_url`` (DNS included) on save and the outbox's
# SSRF check again on send.
#
# A webhook URL is a SECRET: a chat-app incoming-webhook URL is the channel's
# credential. It is stored as Fernet ``url_enc`` + a keyed ``url_fp`` (dedupe,
# and the outbox row's ``target``, so no outbox row holds it) + ``url_hint``
# (``hooks.slack.com/…/••••Xb2``), decrypted only by ``target`` at send time. No
# response carries it: the wire has ``url_hint`` only, and changing the URL is a
# write-only PATCH. A row with plaintext ``url`` (written before this) reads
# transparently and is sealed by the next write (``_prepare`` / ``migrate_legacy``).
#
# The first destination a site adds also switches the ``webhook`` sink on for its
# events, in the same update as the push. A per-destination test send is
# throttled (``TEST_INTERVAL``).

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from pocketpaw_ee.cloud._core.errors import NotFound, RateLimited, ValidationError
from pocketpaw_ee.cloud.leads import notification_settings as ns
from pocketpaw_ee.cloud.models.lead_notifications import (
    DEFAULT_EVENT_SINKS,
    LEAD_EVENTS,
    LEGACY_WEBHOOK_ID,
    MAX_WEBHOOKS,
    LeadWebhook,
    LeadWebhookTemplate,
    SiteNotificationSettings,
)
from pocketpaw_ee.cloud.notifications import webhook_formats, webhook_platforms

WEBHOOK_DISABLE_THRESHOLD = 10
TEST_INTERVAL = timedelta(seconds=30)
LABEL_MAX = 80
UNSET: Any = object()
_LEGACY_FIELDS = (
    "webhook_url",
    "webhook_secret_enc",
    "webhook_secret_prev_enc",
    "webhook_secret_rotated_at",
    "webhook_failure_count",
    "webhook_disabled_at",
)

EVENT_LABELS = {
    "lead_captured": "New lead",
    "handoff": "Handoff to a person",
    "booking": "New booking",
}


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def destinations(settings: SiteNotificationSettings) -> list[LeadWebhook]:
    """The site's destinations, the unmigrated legacy webhook first."""
    out = list(settings.webhooks)
    if settings.webhook_url and not any(d.id == LEGACY_WEBHOOK_ID for d in out):
        out.insert(
            0,
            LeadWebhook(
                id=LEGACY_WEBHOOK_ID,
                url=settings.webhook_url,
                secret_enc=settings.webhook_secret_enc,
                secret_prev_enc=settings.webhook_secret_prev_enc,
                secret_rotated_at=settings.webhook_secret_rotated_at,
                failure_count=settings.webhook_failure_count,
                disabled_at=settings.webhook_disabled_at,
            ),
        )
    return out


def find(settings: SiteNotificationSettings, webhook_id: str) -> LeadWebhook | None:
    return next((d for d in destinations(settings) if d.id == webhook_id), None)


def url_of(dest: LeadWebhook) -> str:
    """The destination's real URL: decrypted, or a pre-encryption plaintext row."""
    if dest.url_enc:
        from pocketpaw_ee.cloud.auth.sso import crypto

        try:
            return crypto.decrypt(dest.url_enc)
        except Exception:  # noqa: BLE001 — a key change: unusable, reads as unset
            return ""
    return dest.url


def fingerprint(url: str) -> str:
    from pocketpaw_ee.cloud.auth.sso import crypto

    return crypto.keyed_digest(url, purpose="lead-webhook-url")


def url_hint(url: str | None) -> str:
    """``https://hooks.slack.com/services/T0/B0/abcdXb2`` -> ``hooks.slack.com/…/••••dXb2``:
    the host and the last 4 characters of path + query, never more."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return "…"
    host = (parsed.hostname or "") + (f":{port}" if port else "")
    rest = ((parsed.path or "") + (f"?{parsed.query}" if parsed.query else "")).rstrip("/")
    return f"{host}/…/••••{rest[-4:]}" if rest else host


def sealed(url: str) -> dict[str, str]:
    """The stored form of ``url``: ciphertext, fingerprint, hint, no plaintext."""
    from pocketpaw_ee.cloud.auth.sso import crypto

    return {
        "url": "",
        "url_enc": crypto.encrypt(url),
        "url_fp": fingerprint(url),
        "url_hint": url_hint(url),
    }


def fp_of(dest: LeadWebhook) -> str:
    return dest.url_fp or fingerprint(url_of(dest))


def hint_of(dest: LeadWebhook) -> str:
    return dest.url_hint or url_hint(url_of(dest))


def row_target(dest: LeadWebhook) -> str:
    """The outbox row's ``target`` for this destination: its fingerprint, never
    the URL. The send re-reads the URL and checks it still has this fingerprint."""
    return f"fp:{fp_of(dest)}"


def platform_of(dest: LeadWebhook) -> str:
    return webhook_platforms.effective_platform(url_of(dest), dest.platform_override)


def is_active(dest: LeadWebhook) -> bool:
    return bool(url_of(dest) and dest.secret_enc and dest.disabled_at is None)


def ref(site_id: str, dest: LeadWebhook) -> str:
    return f"site:{site_id}:{dest.id}"


def parse_ref(rest: str) -> tuple[str, str]:
    """``"<site_id>:<webhook_id>"`` (or the old ``"<site_id>"``) -> ids."""
    site_id, _, webhook_id = rest.partition(":")
    return site_id, webhook_id or LEGACY_WEBHOOK_ID


def wire(dest: LeadWebhook) -> dict[str, Any]:
    """The owner-facing shape. Never the URL: ``url_hint`` only."""
    return {
        "id": dest.id,
        "url_hint": hint_of(dest),
        "label": dest.label,
        "platform": platform_of(dest),
        "detected_platform": webhook_platforms.detect_platform(url_of(dest)),
        "platform_override": dest.platform_override,
        "template": dest.template.model_dump(mode="json"),
        "events": list(dest.events),
        "has_secret": bool(dest.secret_enc),
        "signed": webhook_platforms.is_signed(platform_of(dest)),
        "status": "disabled" if dest.disabled_at is not None else "active",
        "failure_count": dest.failure_count,
        "disabled_at": dest.disabled_at,
        "secret_rotated_at": dest.secret_rotated_at,
        "created_at": dest.created_at,
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _clean_label(label: str | None) -> str:
    text = " ".join(str(label or "").split())
    if len(text) > LABEL_MAX:
        raise ValidationError(
            "lead_notifications.label_too_long", f"Keep the label under {LABEL_MAX} characters."
        )
    return text


def _clean_override(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    if value not in webhook_platforms.PLATFORMS:
        raise ValidationError("lead_notifications.unknown_platform", f"Unknown platform '{value}'.")
    return value


def clean_template(template: dict[str, Any] | LeadWebhookTemplate | None) -> LeadWebhookTemplate:
    if template is None:
        return LeadWebhookTemplate()
    raw = template.model_dump() if isinstance(template, LeadWebhookTemplate) else dict(template)
    title = " ".join(str(raw.get("title") or "").split())
    if len(title) > webhook_formats.TITLE_MAX:
        raise ValidationError(
            "lead_notifications.title_too_long",
            f"Keep the title under {webhook_formats.TITLE_MAX} characters.",
        )
    fields = raw.get("fields")
    if fields is None:
        fields = list(LeadWebhookTemplate().fields)
    bad = [f for f in fields if f not in webhook_formats.TEMPLATE_FIELDS]
    if bad:
        raise ValidationError("lead_notifications.unknown_field", f"Unknown field '{bad[0]}'.")
    return LeadWebhookTemplate(
        title=title,
        fields=[f for f in webhook_formats.TEMPLATE_FIELDS if f in fields],
        show_link=raw.get("show_link") is not False,
    )


def _clean_events(events: list[str] | None) -> list[str]:
    if events is None:
        return list(LEAD_EVENTS)
    bad = [e for e in events if e not in LEAD_EVENTS]
    if bad:
        raise ValidationError("lead_notifications.unknown_event", f"Unknown event '{bad[0]}'.")
    return [e for e in LEAD_EVENTS if e in events]


async def _clean_url(url: str) -> str:
    from pocketpaw_ee.cloud.notifications.delivery import validate_webhook_url

    value = (url or "").strip()
    if not value or len(value) > 2048:
        raise ValidationError("lead_notifications.invalid_webhook_url", "Enter a webhook URL.")
    await validate_webhook_url(value)
    return value


def _mint() -> tuple[str, str]:
    """(plaintext secret, its ciphertext)."""
    from pocketpaw_ee.cloud.audit.webhooks import mint_secret
    from pocketpaw_ee.cloud.auth.sso import crypto

    secret = mint_secret()
    return secret, crypto.encrypt(secret)


# ---------------------------------------------------------------------------
# Writes (each one targeted; see the header)
# ---------------------------------------------------------------------------


async def migrate_legacy(key: dict[str, str]) -> None:
    """Move the flat legacy webhook into ``webhooks`` as id ``legacy``. No-op
    when there is none. The filter pins every value read, so a write that lands
    in between makes this retry with fresh values instead of losing it."""
    coll = ns._coll()
    for _ in range(5):
        raw = await coll.find_one(key, {f: 1 for f in (*_LEGACY_FIELDS, "webhooks")})
        if raw is None or not raw.get("webhook_url"):
            return
        legacy = LeadWebhook(
            id=LEGACY_WEBHOOK_ID,
            **sealed(raw["webhook_url"]),
            secret_enc=raw.get("webhook_secret_enc") or "",
            secret_prev_enc=raw.get("webhook_secret_prev_enc") or "",
            secret_rotated_at=raw.get("webhook_secret_rotated_at"),
            failure_count=int(raw.get("webhook_failure_count") or 0),
            disabled_at=raw.get("webhook_disabled_at"),
        )
        result = await coll.update_one(
            {**key, **{f: raw.get(f) for f in _LEGACY_FIELDS}},
            {
                "$set": {
                    "webhook_url": None,
                    "webhook_secret_enc": "",
                    "webhook_secret_prev_enc": "",
                    "webhook_secret_rotated_at": None,
                    "webhook_failure_count": 0,
                    "webhook_disabled_at": None,
                },
                "$push": {"webhooks": {"$each": [legacy.model_dump()], "$position": 0}},
            },
        )
        if getattr(result, "modified_count", 0):
            return


async def seal_plaintext(key: dict[str, str]) -> None:
    """Encrypt any destination still holding a plaintext ``url`` (written before
    encryption). Each update is conditional on that plaintext, so it is idempotent."""
    coll = ns._coll()
    raw = await coll.find_one(key, {"webhooks": 1}) or {}
    for hook in raw.get("webhooks") or []:
        plain = hook.get("url") or ""
        if not plain or hook.get("url_enc"):
            continue
        await coll.update_one(
            {**key, "webhooks": {"$elemMatch": {"id": hook.get("id"), "url": plain}}},
            {"$set": {f"webhooks.$.{k}": v for k, v in sealed(plain).items()}},
        )


async def _prepare(site) -> SiteNotificationSettings:
    await ns._ensure_row(site)
    await migrate_legacy(ns._key(site))
    await seal_plaintext(ns._key(site))
    return await ns.settings_for(site)


async def add(
    site,
    *,
    url: str,
    label: str = "",
    platform_override: str | None = None,
    template: Any = None,
    events: list[str] | None = None,
    webhook_id: str | None = None,
) -> tuple[LeadWebhook, str]:
    """Add one destination. Returns it and its new plaintext secret."""
    clean_url = await _clean_url(url)
    dest_label = _clean_label(label)
    override = _clean_override(platform_override)
    tpl = clean_template(template)
    dest_events = _clean_events(events)
    current = await _prepare(site)
    fp = fingerprint(clean_url)
    secret, secret_enc = _mint()
    dest = LeadWebhook(
        id=webhook_id or uuid.uuid4().hex[:12],
        **sealed(clean_url),
        label=dest_label,
        platform_override=override,
        template=tpl,
        events=dest_events,
        secret_enc=secret_enc,
        created_at=_now(),
    )
    for _ in range(3):
        if any(fp_of(d) == fp for d in current.webhooks):
            raise _duplicate()
        if len(current.webhooks) >= MAX_WEBHOOKS:
            raise _too_many()
        query: dict[str, Any] = {
            **ns._key(site),
            "webhooks.url_fp": {"$ne": fp},
            f"webhooks.{MAX_WEBHOOKS - 1}": {"$exists": False},
        }
        update: dict[str, Any] = {"$push": {"webhooks": dest.model_dump()}}
        if not current.webhooks:
            # The first destination switches the webhook sink on for its events
            # in this same update, so the owner needn't also flip the matrix.
            query["webhooks.0"] = {"$exists": False}
            await _route_first(site, dest_events, update)
        result = await ns._coll().update_one(query, update)
        if getattr(result, "modified_count", 0) == 1:
            return dest, secret
        current = await ns.settings_for(site)  # lost a race: look again
    raise _too_many()


async def _route_first(site, events: list[str], update: dict[str, Any]) -> None:
    """Add ``webhook`` to each of ``events``' sinks (an event the row doesn't
    list yet gets the defaults plus webhook), into ``update``."""
    stored = (await ns._coll().find_one(ns._key(site), {"events": 1}) or {}).get("events") or {}
    add_to: dict[str, Any] = {}
    set_: dict[str, Any] = {}
    for event in events:
        if event in stored:
            add_to[f"events.{event}"] = "webhook"
        else:
            set_[f"events.{event}"] = [*DEFAULT_EVENT_SINKS, "webhook"]
    if add_to:
        update["$addToSet"] = add_to
    if set_:
        update["$set"] = set_


def _duplicate() -> ValidationError:
    return ValidationError(
        "lead_notifications.duplicate_webhook", "That webhook URL is already added."
    )


def _too_many() -> ValidationError:
    return ValidationError(
        "lead_notifications.too_many_webhooks",
        f"A site can send to at most {MAX_WEBHOOKS} webhooks.",
    )


async def update(
    site,
    webhook_id: str,
    *,
    url: str | None = None,
    label: str | None = None,
    platform_override: Any = UNSET,
    template: Any = None,
    events: list[str] | None = None,
    rearm: bool = False,
) -> tuple[LeadWebhook, str | None, bool]:
    """Patch one destination. Returns (it, a new plaintext secret or None,
    whether that secret should be shown). A new URL mints a secret and re-arms;
    a destination that becomes json gets a fresh secret to show once."""
    current = await _prepare(site)
    dest = find(current, webhook_id)
    if dest is None:
        raise NotFound("lead_notification_webhook", webhook_id)
    before = platform_of(dest)
    sets: dict[str, Any] = {}
    secret: str | None = None
    new_url = url_of(dest)
    if url is not None and url.strip() and url.strip() != new_url:
        new_url = await _clean_url(url)
        new_fp = fingerprint(new_url)
        if any(fp_of(d) == new_fp and d.id != webhook_id for d in current.webhooks):
            raise _duplicate()
        sets.update(sealed(new_url))
        rearm = True
    if label is not None:
        sets["label"] = _clean_label(label)
    override = dest.platform_override
    if platform_override is not UNSET:
        override = _clean_override(platform_override)
        sets["platform_override"] = override
    if template is not None:
        sets["template"] = clean_template(template).model_dump()
    if events is not None:
        sets["events"] = _clean_events(events)
    after = webhook_platforms.effective_platform(new_url, override)
    if "url_enc" in sets or not dest.secret_enc or (after == "json" and before != "json"):
        secret, sets["secret_enc"] = _mint()
        sets.update(secret_prev_enc="", secret_rotated_at=None)
    if rearm:
        sets.update(failure_count=0, disabled_at=None)
    if sets:
        result = await ns._coll().update_one(
            {**ns._key(site), "webhooks.id": webhook_id},
            {"$set": {f"webhooks.$.{k}": v for k, v in sets.items()}},
        )
        if getattr(result, "matched_count", 1) == 0:
            raise NotFound("lead_notification_webhook", webhook_id)
    fresh = find(await ns.settings_for(site), webhook_id) or dest
    return fresh, secret, secret is not None and platform_of(fresh) == "json"


async def remove(site, webhook_id: str) -> None:
    await _prepare(site)
    result = await ns._coll().update_one(ns._key(site), {"$pull": {"webhooks": {"id": webhook_id}}})
    if getattr(result, "modified_count", 0) == 0:
        raise NotFound("lead_notification_webhook", webhook_id)


async def rotate(site, webhook_id: str) -> tuple[LeadWebhook, str]:
    """New signing secret (returned once) and re-arm. The replaced secret keeps
    co-signing for the grace window."""
    from pocketpaw_ee.cloud.auth.sso import crypto

    current = await _prepare(site)
    dest = find(current, webhook_id)
    if dest is None:
        raise NotFound("lead_notification_webhook", webhook_id)
    secret, _ = _mint()
    sets = {
        "secret_prev_enc": dest.secret_enc,
        "secret_enc": crypto.encrypt(secret),
        "secret_rotated_at": _now(),
        "failure_count": 0,
        "disabled_at": None,
    }
    await ns._coll().update_one(
        {**ns._key(site), "webhooks.id": webhook_id},
        {"$set": {f"webhooks.$.{k}": v for k, v in sets.items()}},
    )
    return find(await ns.settings_for(site), webhook_id) or dest, secret


async def record_result(workspace_id: str, site_id: str, webhook_id: str, *, ok: bool) -> None:
    """Atomic: reset on success; on a dead delivery ``$inc`` the counter and, in
    a separate conditional ``$set``, switch the destination off at the threshold."""
    key = {"workspace": workspace_id, "site_id": site_id}
    await migrate_legacy(key)
    coll = ns._coll()
    if ok:
        await coll.update_one(
            {**key, "webhooks": {"$elemMatch": {"id": webhook_id, "failure_count": {"$gt": 0}}}},
            {"$set": {"webhooks.$.failure_count": 0}},
        )
        return
    await coll.update_one(
        {**key, "webhooks.id": webhook_id}, {"$inc": {"webhooks.$.failure_count": 1}}
    )
    await coll.update_one(
        {
            **key,
            "webhooks": {
                "$elemMatch": {
                    "id": webhook_id,
                    "failure_count": {"$gte": WEBHOOK_DISABLE_THRESHOLD},
                    "disabled_at": None,
                }
            },
        },
        {"$set": {"webhooks.$.disabled_at": _now()}},
    )


# ---------------------------------------------------------------------------
# Send time
# ---------------------------------------------------------------------------


@dataclass
class SiteWebhookTarget:
    url: str
    secrets: list[str]
    platform: str
    template: LeadWebhookTemplate = field(default_factory=LeadWebhookTemplate)
    site_id: str = ""
    site_name: str = ""
    # What the outbox row's ``target`` must equal (``row_target``).
    match: str = ""


async def target(workspace_id: str, site_id: str, webhook_id: str) -> SiteWebhookTarget | None:
    """The destination as it is configured NOW, while it is active, else None."""
    from pocketpaw_ee.cloud.notifications.service import signing_secrets

    site = await ns.find_site(workspace_id, site_id)
    if site is None:
        return None
    dest = find(await ns.settings_for(site), webhook_id)
    if dest is None or not is_active(dest):
        return None
    return SiteWebhookTarget(
        url=url_of(dest),
        match=row_target(dest),
        secrets=signing_secrets(dest.secret_enc, dest.secret_prev_enc, dest.secret_rotated_at),
        platform=platform_of(dest),
        template=dest.template,
        site_id=str(site.id),
        site_name=site.name or "",
    )


def event_rows(
    site,
    settings: SiteNotificationSettings,
    *,
    event: str,
    kind: str,
    envelope: dict[str, Any],
    link: str,
    json_only: bool = False,
) -> list[dict[str, Any]]:
    """One outbox row per active destination subscribed to ``event`` (every
    active destination for ``test``)."""
    rows = []
    for dest in destinations(settings):
        if not is_active(dest) or (event != "test" and event not in dest.events):
            continue
        if json_only and platform_of(dest) != "json":
            continue
        rows.append(
            {
                "workspace": site.workspace,
                "kind": kind,
                "sink": "webhook",
                "target": row_target(dest),
                "payload": {**envelope, "link": link},
                "webhook_ref": ref(str(site.id), dest),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Owner-facing operations (the routes)
# ---------------------------------------------------------------------------


async def add_webhook(workspace_id: str, site_id: str, **fields: Any) -> dict[str, Any]:
    """Add a destination. The response is the settings wire plus ``webhook``
    and ``secret`` (shown once, json destinations only; else null)."""
    site = await ns._load(workspace_id, site_id)
    dest, secret = await add(site, **fields)
    out = await ns._wire(site)
    shown = secret if platform_of(dest) == "json" else None
    return {**out, "webhook": wire(dest), "secret": shown}


async def update_webhook(
    workspace_id: str, site_id: str, webhook_id: str, **fields: Any
) -> dict[str, Any]:
    site = await ns._load(workspace_id, site_id)
    dest, secret, show = await update(site, webhook_id, **fields)
    out = await ns._wire(site)
    return {**out, "webhook": wire(dest), "secret": secret if show else None}


async def remove_webhook(workspace_id: str, site_id: str, webhook_id: str) -> dict[str, Any]:
    site = await ns._load(workspace_id, site_id)
    await remove(site, webhook_id)
    return await ns._wire(site)


async def rotate_webhook(workspace_id: str, site_id: str, webhook_id: str) -> dict[str, Any]:
    site = await ns._load(workspace_id, site_id)
    dest, secret = await rotate(site, webhook_id)
    out = await ns._wire(site)
    return {**out, "webhook": wire(dest), "secret": secret}


async def send_webhook_test(workspace_id: str, site_id: str, webhook_id: str) -> dict[str, Any]:
    """Queue one ``notification.test`` delivery to this destination. A switched
    off destination must be re-armed (PATCH ``rearm``) first."""
    from pocketpaw_ee.cloud.notifications import email as email_mod
    from pocketpaw_ee.cloud.notifications import outbox
    from pocketpaw_ee.cloud.notifications.delivery import new_event_envelope

    site = await ns._load(workspace_id, site_id)
    dest = find(await ns.settings_for(site), webhook_id)
    if dest is None:
        raise NotFound("lead_notification_webhook", webhook_id)
    if not is_active(dest):
        raise ValidationError(
            "lead_notifications.webhook_disabled",
            "This webhook is switched off after repeated failures. Re-arm it first.",
        )
    marker = f"webhook_test:{site.workspace}:{site.id}:{dest.id}"
    if not await outbox.claim_marker(marker, TEST_INTERVAL):
        raise RateLimited(
            "lead_notifications.webhook_test_rate_limited",
            "A test was just sent to this webhook. Try again in a few seconds.",
        )
    envelope = new_event_envelope(
        "notification.test", data={"site_id": str(site.id), "site_name": site.name}
    )
    await outbox.enqueue(
        workspace=site.workspace,
        kind="lead_notifications_test",
        sink="webhook",
        target=row_target(dest),
        payload={**envelope, "link": email_mod.lead_url(str(site.id))},
        webhook_ref=ref(str(site.id), dest),
    )
    return {"queued": True, "webhook_id": dest.id, "platform": platform_of(dest)}


async def preview_for(
    workspace_id: str,
    site_id: str,
    *,
    platform: str | None = None,
    url: str | None = None,
    template: Any = None,
    event: str = "lead_captured",
    webhook_id: str | None = None,
) -> dict[str, Any]:
    """Render a sample. ``webhook_id`` previews a saved destination (its platform
    and template unless given), so the client never needs the URL."""
    from pocketpaw_ee.cloud.notifications import email as email_mod

    site = await ns._load(workspace_id, site_id)
    if webhook_id:
        dest = find(await ns.settings_for(site), webhook_id)
        if dest is None:
            raise NotFound("lead_notification_webhook", webhook_id)
        platform = platform or platform_of(dest)
        template = dest.template if template is None else template
    return preview(
        platform=platform,
        url=url,
        template=template,
        event=event,
        site_name=site.name or "",
        link=email_mod.lead_url(str(site.id), "sample"),
    )


# ---------------------------------------------------------------------------
# Preview + catalogue (no network)
# ---------------------------------------------------------------------------


def preview(
    *, platform: str | None, url: str | None, template: Any, event: str, site_name: str, link: str
) -> dict[str, Any]:
    chosen = _clean_override(platform) or webhook_platforms.detect_platform(url)
    if event not in (*LEAD_EVENTS, "test", "lead_updated"):
        raise ValidationError("lead_notifications.unknown_event", f"Unknown event '{event}'.")
    etype = webhook_formats.event_type(event)
    data = webhook_formats.sample_data(event, site_name)
    tpl = clean_template(template)
    body, _headers = webhook_formats.render(
        chosen,
        etype,
        data,
        tpl,
        site_name=site_name,
        link=link,
        event_id="evt_sample",
        created_at="2026-10-10T09:30:00+00:00",
    )
    return {
        "platform": chosen,
        "event": event,
        "signed": chosen == "json",
        "body": body,
        "card": webhook_formats.neutral_card(etype, data, tpl, link=link),
    }


def catalogue() -> dict[str, Any]:
    return {
        "fields": [
            {"id": f, "label": webhook_formats.FIELD_LABELS[f]}
            for f in webhook_formats.TEMPLATE_FIELDS
        ],
        "default_fields": list(LeadWebhookTemplate().fields),
        "platforms": [
            {
                "id": p,
                "label": webhook_platforms.PLATFORM_LABELS[p],
                "signed": webhook_platforms.is_signed(p),
            }
            for p in webhook_platforms.PLATFORMS
        ],
        "events": [
            {
                "id": e,
                "label": EVENT_LABELS[e],
                "default_title": webhook_formats.DEFAULT_TITLES[webhook_formats.event_type(e)],
            }
            for e in LEAD_EVENTS
        ],
        "max_webhooks": MAX_WEBHOOKS,
        "title_max": webhook_formats.TITLE_MAX,
        "label_max": LABEL_MAX,
    }


__all__ = [
    "UNSET",
    "TEST_INTERVAL",
    "WEBHOOK_DISABLE_THRESHOLD",
    "SiteWebhookTarget",
    "add",
    "add_webhook",
    "catalogue",
    "clean_template",
    "destinations",
    "event_rows",
    "find",
    "fingerprint",
    "fp_of",
    "hint_of",
    "is_active",
    "migrate_legacy",
    "parse_ref",
    "platform_of",
    "preview",
    "preview_for",
    "record_result",
    "ref",
    "remove",
    "remove_webhook",
    "rotate",
    "rotate_webhook",
    "row_target",
    "seal_plaintext",
    "sealed",
    "send_webhook_test",
    "target",
    "update",
    "update_webhook",
    "url_hint",
    "url_of",
    "wire",
]
