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
    created_at: str | None


def lead_to_dto(lead: Lead) -> LeadOut:
    return LeadOut(
        id=lead.id,
        site_id=lead.site_id,
        form_type=lead.form_type,
        properties=lead.properties,
        created_at=iso_utc(lead.created_at),
    )


__all__ = ["CaptureRequest", "CaptureResponse", "LeadOut", "lead_to_dto"]
