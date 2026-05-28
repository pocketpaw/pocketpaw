# ee/pocketpaw_ee/cloud/instinct_approvals/dto.py
# Created: 2026-05-28 (feat/wave-3a-instinct-dispatch) — wire-format
# DTOs for the RFC 03 v2 template-level approval queue. Distinct
# request and response classes per EE cloud rule 4 — never reuse one
# model for input and output.

"""Wire-format DTOs for the ``instinct_approvals`` entity."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pocketpaw_ee.cloud._core.time import iso_utc
from pocketpaw_ee.cloud.instinct_approvals.domain import InstinctApproval


class CreateApprovalRequest(BaseModel):
    """Persist a new pending approval row.

    Called by ``pockets.instinct_dispatch.gate_action`` when the
    composer returns ``ESCALATE_APPROVAL``. The route is internal —
    operators do not POST approvals directly; they approve / reject
    existing ones via the decision endpoints.
    """

    model_config = ConfigDict(extra="forbid")

    pocket_id: str = Field(min_length=1)
    action_name: str = Field(min_length=1)
    row_id: str = ""
    row_data: dict[str, Any] = Field(default_factory=dict)
    verdict: str = "ESCALATE_APPROVAL"
    reason: str = ""
    matched_rules: list[dict[str, Any]] = Field(default_factory=list)
    park: dict[str, Any] | None = None


class ListApprovalsRequest(BaseModel):
    """Filter parameters for the list endpoint."""

    model_config = ConfigDict(extra="forbid")

    status: str | None = None
    pocket_id: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class ApprovalDecisionRequest(BaseModel):
    """Approve or reject an existing pending approval.

    Empty body — the route parameter carries the id, the caller's
    workspace + user come from the request context. Kept as a typed
    model so future fields (e.g. reviewer note, batch decision id)
    don't break the wire.
    """

    model_config = ConfigDict(extra="forbid")

    note: str | None = None


class ApprovalResponse(BaseModel):
    """Wire response for one approval row."""

    model_config = ConfigDict(extra="forbid")

    id: str
    workspace_id: str
    pocket_id: str
    action_name: str
    row_id: str
    row_data: dict[str, Any]
    verdict: str
    reason: str
    matched_rules: list[dict[str, Any]]
    requested_at: str | None
    requested_by: str
    status: str
    decided_at: str | None
    decided_by: str | None
    created_at: str | None


def approval_to_dto(a: InstinctApproval) -> ApprovalResponse:
    """Map a domain ``InstinctApproval`` to its wire DTO.

    ``park`` is deliberately not serialized on the wire — it's an
    executor-internal blob (resolved write path / params /
    idempotency_key) that the approval queue UI does not need to see
    and that a future post-approval re-entry consumes server-side.
    """
    return ApprovalResponse(
        id=a.id,
        workspace_id=a.workspace_id,
        pocket_id=a.pocket_id,
        action_name=a.action_name,
        row_id=a.row_id,
        row_data=a.row_data,
        verdict=a.verdict,
        reason=a.reason,
        matched_rules=a.matched_rules,
        requested_at=iso_utc(a.requested_at) if a.requested_at else None,
        requested_by=a.requested_by,
        status=a.status,
        decided_at=iso_utc(a.decided_at) if a.decided_at else None,
        decided_by=a.decided_by,
        created_at=iso_utc(a.created_at) if a.created_at else None,
    )


def approval_to_wire_dict(a: InstinctApproval) -> dict:
    return approval_to_dto(a).model_dump()


__all__ = [
    "ApprovalDecisionRequest",
    "ApprovalResponse",
    "CreateApprovalRequest",
    "ListApprovalsRequest",
    "approval_to_dto",
    "approval_to_wire_dict",
]
