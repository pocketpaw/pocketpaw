"""Cross-tenant Paw Partners switch: an operator turns a workspace into a partner.

Created: 2026-10-02 (feat/partners-foundation, PH-1). Modelled on
``platform/entitlements.py`` (``set_overrides`` / ``clear_overrides``): OPERATOR
rung via ``require_platform``, ``workspace_id`` as an explicit path parameter
under ``/platform``, a required ``reason``, and an audit row wrapped around the
write with ``audit.begin`` / ``audit.settle``.

An ``active`` profile turns the per-site billing seams on for that workspace
(``billing.enforcement.sites_enforced``), which is why this is an operator write.

PUT requires a body (no "empty body clears"); clearing is its own DELETE, the
same split ``clear_overrides`` uses, so a client that drops the body cannot
silently switch a partner's billing off.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.models.workspace import (
    IsoCountry,
    PartnerProfile,
    PartnerStatus,
    PartnerTier,
)
from pocketpaw_ee.cloud.partners import service as partners_service
from pocketpaw_ee.cloud.partners.dto import PartnerProfileOut
from pocketpaw_ee.cloud.platform import audit
from pocketpaw_ee.cloud.workspace import service as workspace_service

router = APIRouter(prefix="/workspaces", tags=["platform"])

_ACTION = "platform.partners.write"
Operator = Annotated[User, Depends(require_platform(_ACTION))]


class PartnerWriteIn(BaseModel):
    """PUT body. ``joined_at`` omitted = keep the existing one (or now, if new)."""

    status: PartnerStatus
    tier: PartnerTier = "bronze"
    footer_name: str = Field(min_length=1, max_length=120)
    billing_country: IsoCountry = "IN"
    founding: bool = False
    joined_at: datetime | None = None
    reason: str = ""


class PartnerClearIn(BaseModel):
    """DELETE body — clearing a partner is still a write and still needs a reason."""

    reason: str = ""


class PartnerStateOut(BaseModel):
    workspace_id: str
    partner: PartnerProfileOut | None


def _audit_dict(profile: PartnerProfile | None) -> dict:
    return profile.model_dump(mode="json") if profile is not None else {}


def _require_reason(reason: str) -> None:
    if not reason.strip():
        raise ValidationError("platform.partners.reason_required", "reason is required")


async def _write(
    workspace_id: str,
    profile: PartnerProfile | None,
    *,
    reason: str,
    request: Request,
    operator: User,
) -> PartnerStateOut:
    # global-read: platform route; workspace_id is the operator's path target.
    before = await partners_service.partner_profile_for_workspace(workspace_id)
    event = await audit.begin(
        operator=operator,
        action=_ACTION,
        reason=reason,
        target_type="workspace_partner",
        target_workspace=workspace_id,
        before=_audit_dict(before),
        request=request,
    )
    ok = False
    try:
        doc = await workspace_service.platform_set_partner_profile(workspace_id, profile)
        ok = True
    finally:
        await audit.settle(event, ok=ok, after=_audit_dict(profile) if ok else {})
    partner = doc.partner
    return PartnerStateOut(
        workspace_id=workspace_id,
        partner=PartnerProfileOut.model_validate(partner, from_attributes=True)
        if partner is not None
        else None,
    )


@router.put("/{workspace_id}/partner", response_model=PartnerStateOut)
async def set_partner(
    workspace_id: str, body: PartnerWriteIn, request: Request, operator: Operator
) -> PartnerStateOut:
    """Set or update a workspace's partner profile."""
    _require_reason(body.reason)
    data = body.model_dump(exclude={"reason"})
    if data["joined_at"] is None:
        # global-read: platform route; keep the original join date across updates.
        current = await partners_service.partner_profile_for_workspace(workspace_id)
        if current is not None:
            data["joined_at"] = current.joined_at
        else:
            del data["joined_at"]  # model default: now
    return await _write(
        workspace_id, PartnerProfile(**data), reason=body.reason, request=request, operator=operator
    )


@router.delete("/{workspace_id}/partner", response_model=PartnerStateOut)
async def clear_partner(
    workspace_id: str, body: PartnerClearIn, request: Request, operator: Operator
) -> PartnerStateOut:
    """Remove a workspace's partner profile (site billing reverts to the global flags)."""
    _require_reason(body.reason)
    return await _write(workspace_id, None, reason=body.reason, request=request, operator=operator)
