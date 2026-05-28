# ee/pocketpaw_ee/cloud/instinct_approvals/service.py
# Created: 2026-05-28 (feat/wave-3a-instinct-dispatch) — sole Beanie
# writer for the ``InstinctApproval`` collection (RFC 03 v2). Module-
# level ``async def`` API per EE cloud rule 5. Every state-mutating
# function:
#   * validates at entry via ``<Request>.model_validate(body)`` (rule 6)
#   * filters reads by ``workspace=workspace_id`` (rule 7)
#   * raises ``CloudError`` subclasses, never ``HTTPException`` (rule 10)
#   * emits an event on the way out (rule 9)
#
# Errors:
#   * unknown approval id → ``NotFound("instinct_approval", id)``
#   * tenant mismatch on read → returns ``None`` (no oracle); on
#     decision attempt → ``NotFound`` (treat as if it does not exist)
#   * already-decided approval → ``ConflictError("instinct_approval.already_decided", ...)``

"""Service for ``instinct_approvals``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import ConflictError, NotFound, ValidationError
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    InstinctApprovalApproved,
    InstinctApprovalCreated,
    InstinctApprovalRejected,
)
from pocketpaw_ee.cloud.instinct_approvals.domain import InstinctApproval
from pocketpaw_ee.cloud.instinct_approvals.dto import (
    ApprovalDecisionRequest,
    CreateApprovalRequest,
    ListApprovalsRequest,
    approval_to_wire_dict,
)
from pocketpaw_ee.cloud.models.instinct_approval import InstinctApproval as _ApprovalDoc

# ---------------------------------------------------------------------------
# Private mapping helper — Beanie doc → domain
# ---------------------------------------------------------------------------


def _to_domain(doc: _ApprovalDoc) -> InstinctApproval:
    return InstinctApproval(
        id=str(doc.id),
        workspace_id=doc.workspace,
        pocket_id=doc.pocket_id,
        action_name=doc.action_name,
        row_id=doc.row_id,
        row_data=dict(doc.row_data or {}),
        verdict=doc.verdict,
        reason=doc.reason,
        matched_rules=list(doc.matched_rules or []),
        requested_at=doc.requested_at,
        requested_by=doc.requested_by,
        status=doc.status,
        decided_at=doc.decided_at,
        decided_by=doc.decided_by,
        park=dict(doc.park) if doc.park else None,
        created_at=getattr(doc, "createdAt", None),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create_approval(
    workspace_id: str, user_id: str, body: dict | CreateApprovalRequest
) -> dict:
    """Persist a new pending approval row.

    Called by ``pockets.instinct_dispatch.gate_action`` when the
    template-level composer returns ``ESCALATE_APPROVAL``. Re-validates
    the body (FastAPI parsed it; internal callers re-parse so the
    schema is enforced uniformly — rule 6).
    """
    body = CreateApprovalRequest.model_validate(body)

    if not workspace_id:
        raise ValidationError(
            "instinct_approval.workspace_required",
            "workspace_id is required to create an approval row",
        )
    if not user_id:
        raise ValidationError(
            "instinct_approval.user_required",
            "user_id is required to create an approval row",
        )

    now = datetime.now(UTC)
    doc = _ApprovalDoc(
        workspace=workspace_id,
        pocket_id=body.pocket_id,
        action_name=body.action_name,
        row_id=body.row_id,
        row_data=body.row_data,
        verdict=body.verdict,
        reason=body.reason,
        matched_rules=body.matched_rules,
        requested_at=now,
        requested_by=user_id,
        status="pending",
        park=body.park,
    )
    await doc.insert()
    domain = _to_domain(doc)
    wire = approval_to_wire_dict(domain)
    await emit(InstinctApprovalCreated(data=dict(wire)))
    return wire


async def list_approvals(
    workspace_id: str, user_id: str, body: dict | ListApprovalsRequest
) -> list[dict]:
    """List approvals scoped to ``workspace_id``. ``user_id`` is the
    viewer; current behaviour is workspace-wide read (no per-user
    filtering) — a future PR adds approver-scoped filtering."""
    body = ListApprovalsRequest.model_validate(body)
    # `user_id` carries viewer context for future per-approver filtering.
    _ = user_id

    query: dict[str, Any] = {"workspace": workspace_id}
    if body.status:
        query["status"] = body.status
    if body.pocket_id:
        query["pocket_id"] = body.pocket_id
    cursor = (
        _ApprovalDoc.find(query).sort(-_ApprovalDoc.createdAt).limit(body.limit)  # type: ignore[operator]
    )
    return [approval_to_wire_dict(_to_domain(doc)) async for doc in cursor]


async def get_approval(workspace_id: str, user_id: str, approval_id: str) -> dict:
    """Return one approval row by id, scoped to ``workspace_id``.

    Raises ``NotFound`` when the id does not resolve in the caller's
    workspace — treating a foreign-workspace hit as a 404 keeps the
    endpoint from being a cross-tenant existence oracle.
    """
    _ = user_id  # viewer context unused on the read path today
    try:
        oid = PydanticObjectId(approval_id)
    except Exception as exc:
        raise NotFound("instinct_approval", approval_id) from exc

    doc = await _ApprovalDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("instinct_approval", approval_id)
    return approval_to_wire_dict(_to_domain(doc))


async def approve(
    workspace_id: str,
    user_id: str,
    approval_id: str,
    body: dict | ApprovalDecisionRequest | None = None,
) -> dict:
    """Mark a pending approval as ``approved``. Emits ``InstinctApprovalApproved``.

    Out of scope for Wave 3a: this PR persists the decision only. The
    follow-up wave wires the post-approval re-entry into
    ``action_executor.run_action(from_instinct=True)``.
    """
    body = ApprovalDecisionRequest.model_validate(body or {})
    return await _decide(
        workspace_id=workspace_id,
        user_id=user_id,
        approval_id=approval_id,
        new_status="approved",
        event_cls=InstinctApprovalApproved,
        note=body.note,
    )


async def reject(
    workspace_id: str,
    user_id: str,
    approval_id: str,
    body: dict | ApprovalDecisionRequest | None = None,
) -> dict:
    """Mark a pending approval as ``rejected``. Emits ``InstinctApprovalRejected``."""
    body = ApprovalDecisionRequest.model_validate(body or {})
    return await _decide(
        workspace_id=workspace_id,
        user_id=user_id,
        approval_id=approval_id,
        new_status="rejected",
        event_cls=InstinctApprovalRejected,
        note=body.note,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _decide(
    *,
    workspace_id: str,
    user_id: str,
    approval_id: str,
    new_status: str,
    event_cls: type,
    note: str | None,
) -> dict:
    if not user_id:
        raise ValidationError(
            "instinct_approval.user_required",
            "user_id is required to decide an approval",
        )
    try:
        oid = PydanticObjectId(approval_id)
    except Exception as exc:
        raise NotFound("instinct_approval", approval_id) from exc

    doc = await _ApprovalDoc.find_one({"_id": oid, "workspace": workspace_id})
    if doc is None:
        raise NotFound("instinct_approval", approval_id)
    if doc.status != "pending":
        raise ConflictError(
            "instinct_approval.already_decided",
            f"approval {approval_id} is already {doc.status!r}",
        )

    doc.status = new_status  # type: ignore[assignment]
    doc.decided_at = datetime.now(UTC)
    doc.decided_by = user_id
    await doc.save()
    domain = _to_domain(doc)
    wire = approval_to_wire_dict(domain)
    payload = dict(wire)
    if note:
        payload["note"] = note
    await emit(event_cls(data=payload))
    return wire


__all__ = [
    "approve",
    "create_approval",
    "get_approval",
    "list_approvals",
    "reject",
]
