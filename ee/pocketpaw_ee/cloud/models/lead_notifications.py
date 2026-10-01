# ee/pocketpaw_ee/cloud/models/lead_notifications.py
# Per-site owner-notification settings, embedded on ``Site.lead_notifications``.
# ``cloud.leads.notification_settings`` is the only writer; the lead bridge and
# the concierge handoff notifier read it to pick who hears about a site event.
#
# ``None`` on the Site means "never configured" and reads as the default: the
# workspace owner's account email plus push for every event. Extra recipients
# (at most 5) must click a signed confirm link before they get any mail; the
# owner's own account address needs no confirm. ``confirm_nonce`` is rotated on
# every (re)add, so a stale link for a removed address can never confirm it.
#
# The webhook secret is Fernet ciphertext (``webhook_secret_enc``), minted when
# the URL is saved and shown once. After 10 consecutive dead deliveries the
# webhook is switched off (``webhook_disabled_at``) until the URL is saved again.

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

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
    confirmed_at: datetime | None = None
    # A permanent bounce reported by the mail provider. A bounced address gets
    # no more mail until it is removed and added (and confirmed) again.
    bounced_at: datetime | None = None


class LeadNotificationSettings(BaseModel):
    # Send to the workspace owner's account email as well as ``emails``.
    include_owner: bool = True
    emails: list[LeadNotificationRecipient] = Field(default_factory=list)
    webhook_url: str | None = None
    webhook_secret_enc: str = ""
    webhook_failure_count: int = 0
    webhook_disabled_at: datetime | None = None
    events: dict[str, list[LeadSink]] = Field(default_factory=default_events)


__all__ = [
    "DEFAULT_EVENT_SINKS",
    "LEAD_EVENTS",
    "MAX_EXTRA_RECIPIENTS",
    "LeadNotificationRecipient",
    "LeadNotificationSettings",
    "LeadSink",
    "default_events",
]
