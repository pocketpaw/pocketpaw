# ee/pocketpaw_ee/cloud/models/lead.py — a captured lead, in the tenant's cloud
# store (NOT local SQLite Fabric). Workspace + site scoped; ``site_id`` is the
# site's ``script_name``. Indexed by (workspace, site_id, createdAt desc) so the
# Leads view pages per site, newest first. Written only by cloud/leads/service.py.
#
# Every field added after launch is optional with a default, so an old row reads
# as status "new", unread, source kind "form" and no conversation.
#   * ``source.kind`` says how the lead arrived: "form" (a site form, the public
#     capture routes), "concierge" (the visitor tapped Send on the concierge's
#     send_to_team card), "handoff" (a handoff that carried a contact) or
#     "booking". ``source.conversation_ref`` ("<widget_id>:<customer_ref>") links
#     a concierge/handoff lead to its transcript. A partial unique index keeps
#     handoff leads to one per (workspace, site, conversation).
#   * ``status`` is the owner's pipeline state; ``read_at`` the first time the
#     owner marked it read (None = unread).
#   * ``source.rate_key`` is the server-derived host hash the per-IP limiter used;
#     ``submitter_ref`` is only an opaque caller label, never a limiter key.
#   * ``source.origin`` / ``origin_unrecognized`` are recorded, not enforced
#     (``Site.enforce_origin`` is opt-in), evaluated against the allowlist at
#     capture time.
# The time index uses ``createdAt``, the column TimestampedDocument defines.

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from beanie import Indexed
from pydantic import BaseModel, Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument

LeadStatus = Literal["new", "contacted", "won", "lost", "booked"]
LeadKind = Literal["form", "concierge", "handoff", "booking"]
LEAD_STATUSES: tuple[str, ...] = ("new", "contacted", "won", "lost", "booked")
LEAD_KINDS: tuple[str, ...] = ("form", "concierge", "handoff", "booking")


class LeadSource(BaseModel):
    """Provenance of a captured lead."""

    form_type: str  # e.g. "AppointmentRequest"
    site_id: str
    submitter_ref: str = ""  # opaque, caller-supplied LABEL (not PII, not the limiter key)
    rate_key: str = ""  # server-derived host hash; the per-IP limiter buckets on this
    # The submitting page's ``Origin``, as sent ("" when the browser sent none).
    #
    # Recorded rather than enforced: since ``Site.enforce_origin`` defaults off, a
    # submission from an unexpected host is ACCEPTED and attributed instead of
    # 403'd. That is the trade the default makes — an owner who wants to know where
    # leads came from can read it here, and one who wants the old hard gate flips
    # ``enforce_origin``. Server-derived (read off the request headers), never a
    # body field, so a caller cannot forge the recorded value independently of the
    # header the browser actually sent.
    origin: str = ""
    # True when an origin WAS sent and it is not on the site's allowlist. Precomputed
    # at capture because the allowlist can change afterwards, and a lead's flag
    # should mean "unrecognized when it arrived" rather than "unrecognized today".
    origin_unrecognized: bool = False
    # How the lead arrived; see the header. Old rows read as "form".
    kind: LeadKind = "form"
    # "<widget_id>:<customer_ref>" for a concierge / handoff / booking lead.
    conversation_ref: str = ""


class Lead(TimestampedDocument):
    """One captured form submission for a published site."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    site_id: Indexed(str)  # type: ignore[valid-type]
    form_type: str
    # Resolved record properties (post event-mapping interpolation).
    properties: dict[str, Any] = Field(default_factory=dict)
    source: LeadSource
    status: LeadStatus = "new"
    read_at: datetime | None = None

    class Settings:
        name = "leads"
        indexes = [
            # ``createdAt`` desc is the per-site Leads list cursor (newest
            # first); the compound index keeps that query cheap once a site
            # accumulates thousands of submissions.
            [("workspace", 1), ("site_id", 1), ("createdAt", -1)],
            # One handoff lead per conversation, enforced by the database so
            # concurrent handoffs can't both insert (the service treats the
            # duplicate-key error as "already captured").
            IndexModel(
                [
                    ("workspace", 1),
                    ("site_id", 1),
                    ("source.kind", 1),
                    ("source.conversation_ref", 1),
                ],
                unique=True,
                partialFilterExpression={"source.kind": "handoff"},
                name="uq_handoff_lead_per_conversation",
            ),
        ]
