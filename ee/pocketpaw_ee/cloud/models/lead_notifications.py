# ee/pocketpaw_ee/cloud/models/lead_notifications.py
# Per-site owner-notification settings, one row per (workspace, site_id) in the
# ``site_notification_settings`` collection. ``cloud.leads.notification_settings``
# and its ``webhook_destinations`` and ``whatsapp_numbers`` helpers are the only
# writers; the lead bridge,
# the concierge handoff notifier and the outbox read through those modules.
#
# It lives in its OWN collection, not on the Site document, on purpose: the
# sites service saves whole Site documents (``site.save()``), and a Site loaded
# before a confirm click or a failure-counter bump would write the stale
# settings straight back. Here every write is a targeted ``$set`` / ``$inc`` /
# ``$push`` / ``$pull`` / positional update on this row, so concurrent writers
# can't lose updates.
#
# No row means "never configured" and reads as the default: the workspace
# owner's account email plus push for every event. An owner address that the
# account hasn't verified must confirm through the same link as an extra
# recipient (``owner_confirm``) first. Extra recipients (at most 5, stored
# lower-cased) must click a signed confirm link before they get mail;
# ``confirm_nonce`` is rotated on every (re)send, so a stale link can't confirm.
#
# Webhooks: ``webhooks`` holds up to ``MAX_WEBHOOKS`` destinations, each with its
# own URL, platform override, message template, events, Fernet-encrypted signing
# secret (the previous one co-signs for a grace window after a rotation) and
# failure counter (10 consecutive dead deliveries switch that one destination
# off until it is re-armed). The flat ``webhook_*`` fields are the pre-list
# single webhook: readers see it as destination id ``legacy`` and the first
# webhook write moves it into the list. Nothing else writes them.
#
# WhatsApp to the owner: ``whatsapp.numbers`` holds up to ``MAX_WHATSAPP_NUMBERS``
# E.164 numbers, each stored with the consent time of the person who added it
# (``leads.whatsapp_numbers`` is their writer). The ``whatsapp_owner`` sink in
# ``events`` sends an event to every listed number.
#
# ``LeadSink`` also includes "whatsapp" for partner leads: those are routed by
# the shop owner's consent, not by these settings, so ``events`` never lists it
# and the settings API doesn't accept it.

from __future__ import annotations

from datetime import datetime
from typing import Literal

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import IndexModel

# The site events an owner can route, and the sinks each can go to.
LEAD_EVENTS: tuple[str, ...] = ("lead_captured", "handoff", "booking")
LeadSink = Literal["email", "webhook", "push", "whatsapp", "whatsapp_owner"]
DEFAULT_EVENT_SINKS: list[str] = ["email", "push"]
MAX_EXTRA_RECIPIENTS = 5
MAX_WEBHOOKS = 5
MAX_WHATSAPP_NUMBERS = 3
LEGACY_WEBHOOK_ID = "legacy"
WebhookPlatform = Literal["slack", "discord", "teams", "google_chat", "json"]
DEFAULT_TEMPLATE_FIELDS: tuple[str, ...] = ("name", "email", "phone", "message", "page")


def default_events() -> dict[str, list[str]]:
    return {event: list(DEFAULT_EVENT_SINKS) for event in LEAD_EVENTS}


class LeadNotificationRecipient(BaseModel):
    email: str
    added_at: datetime
    added_by: str = ""
    confirm_nonce: str = ""
    confirm_sent_at: datetime | None = None
    confirmed_at: datetime | None = None
    # A permanent bounce reported by the mail provider. A bounced address gets
    # no more mail until it is removed and added (and confirmed) again.
    bounced_at: datetime | None = None


class LeadWebhookTemplate(BaseModel):
    # "" means the event's default title.
    title: str = ""
    fields: list[str] = Field(default_factory=lambda: list(DEFAULT_TEMPLATE_FIELDS))
    show_link: bool = True


class LeadWebhook(BaseModel):
    id: str
    url: str
    label: str = ""
    # None: use the platform detected from the URL.
    platform_override: WebhookPlatform | None = None
    template: LeadWebhookTemplate = Field(default_factory=LeadWebhookTemplate)
    # Which site events this destination gets (the routing matrix's "webhook"
    # column must also be on for the event). ``lead.updated`` follows lead_captured.
    events: list[str] = Field(default_factory=lambda: list(LEAD_EVENTS))
    secret_enc: str = ""
    secret_prev_enc: str = ""
    secret_rotated_at: datetime | None = None
    failure_count: int = 0
    disabled_at: datetime | None = None
    created_at: datetime | None = None


class LeadWhatsAppNumber(BaseModel):
    # E.164: "+" then 8-15 digits.
    e164: str
    added_at: datetime
    added_by: str = ""
    # When the adder confirmed this number may get lead messages.
    consent_at: datetime


class LeadWhatsApp(BaseModel):
    numbers: list[LeadWhatsAppNumber] = Field(default_factory=list)


class SiteNotificationSettings(Document):
    workspace: str
    site_id: str
    # Send to the workspace owner's account email as well as ``emails``.
    include_owner: bool = True
    # Confirm state for an owner whose ACCOUNT email isn't verified (e.g. a
    # password signup): that address goes through the same confirm link as an
    # extra recipient before it gets mail. Unused while the account is verified.
    owner_confirm: LeadNotificationRecipient | None = None
    emails: list[LeadNotificationRecipient] = Field(default_factory=list)
    webhooks: list[LeadWebhook] = Field(default_factory=list)
    whatsapp: LeadWhatsApp = Field(default_factory=LeadWhatsApp)
    # The pre-list single webhook (read as destination ``legacy``; see the header).
    webhook_url: str | None = None
    webhook_secret_enc: str = ""
    webhook_secret_prev_enc: str = ""
    webhook_secret_rotated_at: datetime | None = None
    webhook_failure_count: int = 0
    webhook_disabled_at: datetime | None = None
    events: dict[str, list[LeadSink]] = Field(default_factory=default_events)

    class Settings:
        name = "site_notification_settings"
        indexes = [IndexModel([("workspace", 1), ("site_id", 1)], unique=True)]


__all__ = [
    "DEFAULT_EVENT_SINKS",
    "DEFAULT_TEMPLATE_FIELDS",
    "LEAD_EVENTS",
    "LEGACY_WEBHOOK_ID",
    "MAX_EXTRA_RECIPIENTS",
    "MAX_WEBHOOKS",
    "MAX_WHATSAPP_NUMBERS",
    "LeadNotificationRecipient",
    "LeadSink",
    "LeadWebhook",
    "LeadWebhookTemplate",
    "LeadWhatsApp",
    "LeadWhatsAppNumber",
    "SiteNotificationSettings",
    "WebhookPlatform",
    "default_events",
]
