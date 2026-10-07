"""Cross-tenant Paw Partners operator routes: the partner switch and the
application queue.

``router`` (``/platform/workspaces/{id}/partner``) turns a workspace into a
partner; ``applications_router`` (``/platform/partners/applications``) lists and
reviews the public ``POST /partners/apply`` submissions through
``partners.service_admin`` (never the Beanie doc; the PartnerApplications
import-linter contract binds this module). Both are modelled on
``platform/entitlements.py``: OPERATOR rung via ``require_platform``
(``platform.partners.write`` for every route here), targets as explicit path
parameters under ``/platform``, a required ``reason`` on writes, and an audit row
wrapped around each write with ``audit.begin`` / ``audit.settle``; the list read
is recorded with ``audit.record_read``.

An ``active`` profile turns the per-site billing seams on for that workspace
(``billing.enforcement.sites_enforced``), which is why this is an operator write.

PUT requires a body (no "empty body clears"); clearing is its own DELETE, the
same split ``clear_overrides`` uses, so a client that drops the body cannot
silently switch a partner's billing off.

The volume ``tier`` is system-owned (``partners.service.refresh_standing``). The
PUT can still set it, a manual promotion, and it stands until the next recompute
moves it: an upgrade after a sale / client payment, or the monthly review. The
PUT keeps the profile's ``tier_reviewed_at``, so a promotion is reviewed at the
next month boundary rather than within minutes, and keeps the partner's own
public-profile fields (slug, display name, ...), which only the partner edits
through PATCH /partners/me/profile.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
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
from pocketpaw_ee.cloud.partners import service_admin as partners_admin
from pocketpaw_ee.cloud.partners.dto import (
    PartnerApplicationOut,
    PartnerApplicationPage,
    PartnerApplicationReviewIn,
    PartnerApplicationStatus,
    PartnerProfileOut,
)
from pocketpaw_ee.cloud.partners.service import PUBLIC_PROFILE_FIELDS
from pocketpaw_ee.cloud.platform import audit
from pocketpaw_ee.cloud.workspace import service as workspace_service

router = APIRouter(prefix="/workspaces", tags=["platform"])
applications_router = APIRouter(prefix="/partners", tags=["platform"])

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
    # global-read: platform route; keep the original join date and tier review.
    current = await partners_service.partner_profile_for_workspace(workspace_id)
    if current is not None:
        data["tier_reviewed_at"] = current.tier_reviewed_at
        # The partner's own public profile is not the operator's to reset.
        data.update(current.model_dump(include=PUBLIC_PROFILE_FIELDS))
    if data["joined_at"] is None:
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


# ---------------------------------------------------------------- application queue


@applications_router.get("/applications", response_model=PartnerApplicationPage)
async def list_applications(
    request: Request,
    operator: Operator,
    status: Annotated[PartnerApplicationStatus | None, Query()] = None,
    cursor: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> PartnerApplicationPage:
    """The public partner applications, newest first, with the applicant's
    contact details (this is the review queue); ``status`` filters."""
    page = await partners_admin.list_applications(status=status, cursor=cursor, limit=limit)
    await audit.record_read(
        operator=operator,
        action=_ACTION,
        query=f"status={status!r} cursor={cursor!r} limit={limit}",
        target_type="partner_application",
        request=request,
    )
    return page


@applications_router.patch("/applications/{application_id}", response_model=PartnerApplicationOut)
async def review_application(
    application_id: str, body: PartnerApplicationReviewIn, request: Request, operator: Operator
) -> PartnerApplicationOut:
    """Set an application's status (new | contacted | rejected | accepted) and note.
    Accepting is a decision, not a side effect: the applicant's workspace still
    becomes a partner through PUT /platform/workspaces/{id}/partner."""
    _require_reason(body.reason)
    before = await partners_admin.get_application(application_id)  # 404 before any audit row
    event = await audit.begin(
        operator=operator,
        action=_ACTION,
        reason=body.reason,
        target_type="partner_application",
        before={"application_id": application_id, "status": before.status},
        request=request,
    )
    ok = False
    try:
        out = await partners_admin.review_application(
            application_id, status=body.status, note=body.note, reviewed_by=str(operator.id)
        )
        ok = True
    finally:
        await audit.settle(
            event,
            ok=ok,
            after={"application_id": application_id, "status": body.status, "note": body.note}
            if ok
            else {},
        )
    return out
