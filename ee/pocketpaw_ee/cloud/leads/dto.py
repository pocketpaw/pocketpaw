# ee/pocketpaw_ee/cloud/leads/dto.py — request/response DTOs for the leads
# routes. CaptureRequest is the public ingest shape (drained from the edge
# Queue). LeadOut is the Leads view's read shape (list and PATCH). LeadUpdate is
# the owner's PATCH body (status and/or read); ReadAllResponse counts what
# read-all marked.

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.leads.domain import Lead


class CaptureRequest(BaseModel):
    form_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    submitter_ref: str = ""
    signed_key: str  # per-site key; checked against Site.signed_key


class CaptureResponse(BaseModel):
    ok: bool
    lead_id: str | None = None
    reason: str | None = None


class LeadOut(BaseModel):
    id: str
    site_id: str
    form_type: str
    properties: dict[str, Any]
    # Where the submission came from, and whether that host was on the site's
    # allowlist AT CAPTURE TIME. Both are informational — since origin enforcement
    # is opt-in (``Site.enforce_origin``), an unrecognized origin is an accepted
    # lead the owner can judge for themselves rather than one we silently refused.
    origin: str = ""
    origin_unrecognized: bool = False
    # How it arrived: form | concierge | handoff | booking ("form" for old rows).
    source_kind: str = "form"
    # "<widget_id>:<customer_ref>" for a concierge / handoff lead, else "".
    conversation_ref: str = ""
    status: str = "new"
    # ISO-8601 UTC of the first time the owner marked it read; None = unread.
    read_at: str | None = None
    created_at: str | None = None


class LeadUpdate(BaseModel):
    """PATCH body. Send either or both; ``read`` true marks it read (keeping the
    first read time), false marks it unread."""

    status: Literal["new", "contacted", "won", "lost", "booked"] | None = None
    read: bool | None = None


class ReadAllResponse(BaseModel):
    updated: int


def lead_to_dto(lead: Lead) -> LeadOut:
    return LeadOut(
        id=lead.id,
        site_id=lead.site_id,
        form_type=lead.form_type,
        properties=lead.properties,
        origin=lead.origin,
        origin_unrecognized=lead.origin_unrecognized,
        source_kind=lead.source_kind,
        conversation_ref=lead.conversation_ref,
        status=lead.status,
        read_at=iso_utc(lead.read_at),
        created_at=iso_utc(lead.created_at),
    )


__all__ = [
    "CaptureRequest",
    "CaptureResponse",
    "LeadOut",
    "LeadUpdate",
    "ReadAllResponse",
    "lead_to_dto",
]
