# ee/pocketpaw_ee/cloud/models/lead_notifications.py
# Per-site owner-notification settings, one row per (workspace, site_id) in the
# ``site_notification_settings`` collection. ``cloud.leads.notification_settings``
# is the only writer; the lead bridge, the concierge handoff notifier and the
# outbox read it through that module.
#
# It lives in its OWN collection, not on the Site document, on purpose: the
# sites service saves whole Site documents (``site.save()``), and a Site loaded
# before a confirm click or a failure-counter bump would write the stale
# settings straight back. Here every write is a targeted ``$set`` / ``$inc`` /
# ``$push`` / ``$pull`` on this row, so concurrent writers can't lose updates.
#
# No row means "never configured" and reads as the default: the workspace
# owner's account email plus push for every event. An owner address that the
# account hasn't verified must confirm through the same link as an extra
# recipient (``owner_confirm``) first. Extra recipients
# (at most 5, stored lower-cased) must click a signed confirm link before they
# get mail; ``confirm_nonce`` is rotated on every (re)send, so a stale link
# can't confirm. The webhook secret is Fernet ciphertext; after a rotation the
# previous one keeps signing for a grace window. After 10 consecutive dead
# deliveries the webhook is switched off until it is saved again.

from __future__ import annotations

from datetime import datetime
from typing import Literal

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import IndexModel

# The site events an owner can route, and the sinks each can go to.
LEAD_EVENTS: tuple[str, ...] = ("lead_captured", "handoff", "booking")
LeadSink = Literal["email", "webhook", "push"]
DEFAULT_EVENT_SINKS: list[str] = ["email", "push"]
MAX_EXTRA_RECIPIENTS = 5


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
    "LEAD_EVENTS",
    "MAX_EXTRA_RECIPIENTS",
    "LeadNotificationRecipient",
    "LeadSink",
    "SiteNotificationSettings",
    "default_events",
]
