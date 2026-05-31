# ee/pocketpaw_ee/cloud/models/instinct_approval.py
# Created: 2026-05-28 (feat/wave-3a-instinct-dispatch) — Beanie document
# backing the EE-side approval queue for RFC 03 v2 template-level
# Instinct decisions. The pure ``resolve_instinct`` composer in OSS
# returns an ``InstinctDecision`` per row; when the verdict is
# ``ESCALATE_APPROVAL`` the dispatch wrapper persists one row of this
# doc via ``instinct_approvals.service.create_approval`` and the
# action_executor returns a ``code:instinct_pending`` sentinel carrying
# the approval id.
#
# Tenancy: ``workspace`` is required + indexed. Every read in
# ``instinct_approvals/service.py`` filters by it, so an approval row in
# workspace A is invisible to workspace B.
#
# ``_park``: the executor parks the resolved write blob (method / path /
# params / idempotency_key) here so a future post-approval re-entry can
# replay the write without re-resolving the row. This sibling of the
# M2b.1 ``_park`` field on ``pocketpaw.instinct.models.Action`` is
# scoped to the RFC 03 v2 template flow only — the M2b.1 binding-level
# park remains for backward compatibility.

"""Beanie document for the Instinct approval queue (RFC 03 v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument

ApprovalStatusT = Literal["pending", "approved", "rejected", "expired"]


class InstinctApproval(TimestampedDocument):
    """One pending / decided template-level Instinct approval row.

    A row is created when the RFC 03 v2 dispatch wrapper evaluates a
    template-level ``resolve_instinct`` and the verdict is
    ``ESCALATE_APPROVAL``. The action_executor returns the parked write
    to the caller as ``code:instinct_pending`` carrying ``approval_id``;
    the operator approves / rejects via the
    ``/instinct-approvals/{id}/{approve,reject}`` endpoints.

    Fields:
        workspace: tenant id. Indexed; every read filters by it.
        pocket_id: pocket the action fired from.
        action_name: ``ActionDef.name`` slug.
        row_id: identifier of the row the action targeted (free-form).
        row_data: snapshot of the row at decision time. Stored so the
            approval queue UI can render context without re-fetching.
        verdict: copied from the composer's ``InstinctDecision.verdict``.
            Always ``ESCALATE_APPROVAL`` at creation; the field is
            retained for audit symmetry.
        reason: the composer's machine-readable reason code
            (``operator_overlay_escalated`` / ``author_floor``).
        matched_rules: dump of the composer's ``matched_rules`` (when /
            action pairs) so the approval UI can show why.
        requested_at: datetime the gate ran. Distinct from ``createdAt``
            (which Beanie set on insert) so a future migration that moves
            persistence off the request path keeps the timeline accurate.
        requested_by: user id that triggered the action.
        status: ``pending`` on insert; flipped to ``approved`` /
            ``rejected`` on operator decision; ``expired`` reserved for a
            future temporal sweeper.
        decided_at / decided_by: set when an operator approves or
            rejects. ``None`` while pending.
        _park: the resolved write blob the executor parked (method,
            path, params, idempotency_key, outcome, workspace_id,
            requested_by). Re-loaded by a future post-approval re-entry
            into ``run_action(from_instinct=True)``. ``None`` when the
            executor was not threaded through a write (e.g. a future
            non-write action surface).
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: Indexed(str)  # type: ignore[valid-type]
    action_name: str
    row_id: str = ""
    row_data: dict[str, Any] = Field(default_factory=dict)
    verdict: str = "ESCALATE_APPROVAL"
    reason: str = ""
    matched_rules: list[dict[str, Any]] = Field(default_factory=list)
    requested_at: datetime
    requested_by: str
    status: ApprovalStatusT = "pending"
    decided_at: datetime | None = None
    decided_by: str | None = None
    park: dict[str, Any] | None = Field(default=None, alias="_park")

    model_config = {"populate_by_name": True}

    class Settings:
        name = "instinct_approvals"
        indexes = [
            [("workspace", 1), ("status", 1), ("createdAt", -1)],
            [("workspace", 1), ("pocket_id", 1), ("status", 1)],
        ]


__all__ = ["ApprovalStatusT", "InstinctApproval"]
