# ee/pocketpaw_ee/cloud/leads/dto.py — request/response DTOs for the capture
# entity. CaptureRequest is the public ingest shape (drained from the edge
# Queue). LeadOut is the read shape for the Leads view.
#
# Created 2026-05-30 (feat/paw-sites-backend, RFC 12 Task 3.4): wire DTOs for
# the capture ingest + tenant-scoped leads router.

from __future__ import annotations

from typing import Any

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
    created_at: str | None = None


def lead_to_dto(lead: Lead) -> LeadOut:
    return LeadOut(
        id=lead.id,
        site_id=lead.site_id,
        form_type=lead.form_type,
        properties=lead.properties,
        origin=lead.origin,
        origin_unrecognized=lead.origin_unrecognized,
        created_at=iso_utc(lead.created_at),
    )


__all__ = ["CaptureRequest", "CaptureResponse", "LeadOut", "lead_to_dto"]
