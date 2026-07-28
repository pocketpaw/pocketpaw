# ee/pocketpaw_ee/cloud/growth/dto.py — request/response DTOs for the prospect
# entity. Distinct Request and Response shapes per the ee/cloud rule — never
# reuse a model for input and output. ``domain`` (the dedupe key) is normalised
# to a bare lowercase hostname at the DTO boundary so every caller — router,
# upsert, later ingestion slices — dedupes on the same canonical form.
#
# Created 2026-07-27 (feat/growth-g1): first slice of /growth — the prospect
# store. Domain → DTO mapping lives in ``service.py`` as private helpers.
# Updated 2026-07-27 (feat/growth-g2): bulk-ingestion DTOs — BulkIngestRequest
# (raw rows, 500-row cap enforced at the DTO boundary so an oversized payload
# 422s before any row is touched), BulkRowError, BulkIngestResponse. Rows are
# deliberately ``dict`` (not CreateProspectRequest) so one invalid row becomes
# a per-row error entry in the response instead of failing the whole payload.
# Updated 2026-07-27 (feat/growth-g3): draft DTOs — CreateDraftRequest (subject
# is email-only, enforced here at the boundary; body non-empty),
# TransitionDraftRequest (the target status; legality is the SERVICE's job —
# the DTO only checks it's a known status), DraftResponse.
# Updated 2026-07-27 (feat/growth-g4): ProposeSendResponse — the Instinct
# proposal id + the flipped draft returned by POST /growth/drafts/{id}/propose.
# Updated 2026-07-28 (feat/growth-api-scale): ProspectPageResponse — the
# cursor-paginated envelope GET /growth/prospects now returns
# ({items, next_cursor, total}) in place of the bare array. Breaking on
# purpose: "n of m" needs a filter-scoped total and reaching row 3,000 needs a
# resume key, and neither fits in a naked list. ProspectFacetsResponse — the
# per-tier / per-status / per-source counts behind the filter chips.
# Updated 2026-07-27 (feat/growth-g8): LinkedInQueueItemResponse — one row of
# the manual LinkedIn send queue: the draft envelope joined with the prospect
# context the captain needs to send by hand (name, company, profile URL,
# research brief, tier). Response-only; the queue has no request DTO.

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from pocketpaw_ee.cloud.growth.domain import (
    DraftChannel,
    DraftStatus,
    DraftVariant,
    ProspectSource,
    ProspectStatus,
    ProspectTier,
)


def _normalise_domain(v: str) -> str:
    """Canonicalise a company-website domain for the dedupe key.

    Lowercases, strips whitespace, drops an ``http(s)://`` scheme, a ``www.``
    prefix, and any path/port suffix — so ``https://www.Acme.com/about`` and
    ``acme.com`` dedupe to the same row.
    """
    v = v.strip().lower()
    for scheme in ("https://", "http://"):
        if v.startswith(scheme):
            v = v[len(scheme) :]
            break
    v = v.split("/", 1)[0].split(":", 1)[0]
    if v.startswith("www."):
        v = v[len("www.") :]
    return v


class CreateProspectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    company: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=253)
    source: ProspectSource
    tier: ProspectTier = "unqualified"
    research_brief: str = ""
    emails: list[str] = Field(default_factory=list)
    linkedin_url: str | None = None
    whatsapp_number: str | None = None
    opted_in: bool = False
    status: ProspectStatus = "new"

    @field_validator("domain")
    @classmethod
    def _clean_domain(cls, v: str) -> str:
        cleaned = _normalise_domain(v)
        if not cleaned:
            raise ValueError("domain must contain a hostname")
        return cleaned


class UpdateProspectRequest(BaseModel):
    """Partial update — every field optional; ``None`` means "leave as-is".

    ``domain`` and ``source`` are deliberately NOT updatable: the domain is
    the dedupe identity (changing it is a delete+create, not an edit) and the
    source records provenance at capture time.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    company: str | None = Field(default=None, min_length=1, max_length=200)
    tier: ProspectTier | None = None
    research_brief: str | None = None
    emails: list[str] | None = None
    linkedin_url: str | None = None
    whatsapp_number: str | None = None
    opted_in: bool | None = None
    status: ProspectStatus | None = None


class BulkIngestRequest(BaseModel):
    """Bulk-ingestion payload: up to 500 CreateProspectRequest-shaped rows.

    Rows stay ``dict`` on purpose — the service validates each row
    individually so a bad row records an error entry while the rest proceed
    (no all-or-nothing abort). The 500-row cap IS enforced here: an oversized
    payload is a 422 before any row is processed.
    """

    rows: list[dict[str, Any]] = Field(max_length=500)


class BulkRowError(BaseModel):
    """One failed row in a bulk ingest: its position and why it was skipped."""

    index: int
    code: str
    message: str


class BulkIngestResponse(BaseModel):
    created: int
    updated: int
    errors: list[BulkRowError]


class ProspectResponse(BaseModel):
    id: str
    workspace_id: str
    name: str
    company: str
    domain: str
    source: str
    tier: str
    research_brief: str
    emails: list[str]
    linkedin_url: str | None
    whatsapp_number: str | None
    opted_in: bool
    status: str
    created_at: str | None
    updated_at: str | None


class ProspectPageResponse(BaseModel):
    """One cursor-paginated page of prospects (G-10a).

    Replaces the bare array the list route used to return — a BREAKING change,
    made deliberately: without ``total`` the UI cannot say "n of m", and
    without ``next_cursor`` it cannot reach row 3,000. ``total`` counts every
    row matching the CURRENT filters (not the page), so it is stable while
    paging and is what the count reads from.
    """

    items: list[ProspectResponse]
    next_cursor: str | None = None
    total: int


class ProspectFacetsResponse(BaseModel):
    """Counts per tier / status / source for the filter chips (G-10a).

    Each block is ``{value: count}`` covering EVERY legal value, zeros
    included — the chip row keeps a stable shape as the user filters instead
    of chips appearing and vanishing. Each block respects the OTHER active
    filters but not its own, so the tier counts stay meaningful while a status
    filter is on (that is the whole point of a facet).
    """

    tier: dict[str, int]
    status: dict[str, int]
    source: dict[str, int]


class CreateDraftRequest(BaseModel):
    """One channel's outreach copy for a prospect.

    A draft is always born in ``status="draft"`` — there is no status field
    here; lifecycle moves happen only through the transition route.
    """

    channel: DraftChannel
    subject: str | None = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)
    variant: DraftVariant = "first_touch"
    demo_url: str | None = Field(default=None, max_length=2048)

    @field_validator("body")
    @classmethod
    def _non_blank_body(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body must not be blank")
        return v

    @model_validator(mode="after")
    def _subject_is_email_only(self) -> CreateDraftRequest:
        if self.channel != "email" and self.subject is not None:
            raise ValueError("subject is only valid on the email channel")
        return self


class TransitionDraftRequest(BaseModel):
    """Target status for a lifecycle move. Whether the move is LEGAL from the
    draft's current status is the service's call (``draft.illegal_transition``,
    422) — this DTO only rejects unknown status names."""

    status: DraftStatus


class DraftResponse(BaseModel):
    id: str
    workspace_id: str
    prospect_id: str
    channel: str
    subject: str | None
    body: str
    variant: str
    status: str
    demo_url: str | None
    created_at: str | None
    updated_at: str | None


class ProposeSendResponse(BaseModel):
    """Result of proposing a draft for sending (G-4): the Instinct proposal id
    a human approves/rejects in the Tray, plus the draft (now ``proposed``)."""

    proposal_id: str
    draft: DraftResponse


class LinkedInQueueItemResponse(BaseModel):
    """One row of the manual LinkedIn send queue.

    The draft envelope plus the prospect context needed to send it by hand —
    the queue exists so the captain can copy-paste; there is no LinkedIn API
    integration by design (account-ban avoidance).
    """

    draft: DraftResponse
    prospect_name: str
    prospect_company: str
    linkedin_url: str | None
    research_brief: str
    tier: str


__all__ = [
    "BulkIngestRequest",
    "BulkIngestResponse",
    "BulkRowError",
    "CreateDraftRequest",
    "CreateProspectRequest",
    "DraftResponse",
    "LinkedInQueueItemResponse",
    "ProposeSendResponse",
    "ProspectFacetsResponse",
    "ProspectPageResponse",
    "ProspectResponse",
    "TransitionDraftRequest",
    "UpdateProspectRequest",
]
