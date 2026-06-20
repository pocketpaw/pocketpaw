# dto.py — Request/response schemas for the member-ingest worker.
# Created: 2026-06-08 — VIP Onboarding Phase B (per-user ingest worker).
# Updated: 2026-06-08 — chunk 7 (purge path): added PurgeMemberRequest, the
#   input to ``purge.purge_member_data``. Same non-blank guards as
#   IngestMemberRequest so a blank member_id can never collapse the derived
#   ``user:`` scope and clear the wrong (or every) member's KB.
# Per cloud rule §4 (request/response split) and §6 (validate at entry): the
# service functions re-parse their inputs through these even when called by
# internal callers (the sweep, the scheduler, jobs) — not just HTTP.

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class IngestMemberRequest(BaseModel):
    """Input to ``ingest_member`` — the tenant + member to sync.

    ``member_id`` is the opaque cloud user id; it becomes the ``user:{id}``
    KB scope verbatim, so we forbid empty/whitespace ids that would collapse
    to the bare ``user:`` scope and leak across members.
    """

    workspace_id: str = Field(min_length=1)
    member_id: str = Field(min_length=1)

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        if not self.workspace_id.strip():
            raise ValueError("workspace_id must not be blank")
        if not self.member_id.strip():
            raise ValueError("member_id must not be blank")


class SweepRequest(BaseModel):
    """Input to ``run_ingest_sweep`` — the concurrency cap for the fan-out."""

    concurrency: int = Field(default=4, ge=1, le=64)


class PurgeMemberRequest(BaseModel):
    """Input to ``purge_member_data`` — the tenant + member to wipe.

    ``member_id`` is the opaque cloud user id; it becomes the ``user:{id}`` KB
    scope verbatim, exactly as the ingest path derives it. A blank id is
    forbidden because the bare ``user:`` scope would target every member's
    private KB — the single most dangerous mistake this whole chunk exists to
    prevent — so the guard mirrors IngestMemberRequest's.
    """

    workspace_id: str = Field(min_length=1)
    member_id: str = Field(min_length=1)

    def __init__(self, **data: object) -> None:
        super().__init__(**data)
        if not self.workspace_id.strip():
            raise ValueError("workspace_id must not be blank")
        if not self.member_id.strip():
            raise ValueError("member_id must not be blank")


class MemberIngestStatusResponse(BaseModel):
    """Wire shape for a per-member ingest status row (for a future status
    endpoint / operator visibility). Distinct from the request models above."""

    workspace_id: str
    member_id: str
    status: str
    backfill_done: bool
    last_sync_at: datetime | None = None
    last_error: str = ""
    documents_ingested: int = 0
