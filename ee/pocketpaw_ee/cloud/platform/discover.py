"""Staff moderation for the Discover index, on the platform axis.

Created 2026-10-02 (feat/discover-moderation) — DS-5 of the /discover PRD.

Mounted under ``/api/v1/platform/discover``:

  GET  /discover                         platform.discover.read (SUPPORT)
  POST /discover/{listing_id}/feature    platform.discover.moderate (OPERATOR)
  POST /discover/{listing_id}/unfeature  platform.discover.moderate
  POST /discover/{listing_id}/hide       platform.discover.moderate
  POST /discover/{listing_id}/unhide     platform.discover.moderate
  POST /discover/reindex?source=...      platform.discover.moderate

Every route goes through ``discover.service_admin`` (never a Beanie doc; the
Discover import-linter contracts bind this module). Writes follow credits.py:
an operator-supplied ``reason`` is required, ``audit.begin`` records the row as
``attempted`` before the change and ``audit.settle`` closes it. The listing id
goes in ``before`` / ``after`` (the audit model has no target-id field) and the
owner's workspace in ``target_workspace``. The list read is recorded with
``audit.record_read``, like the credits reads.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.discover import service_admin
from pocketpaw_ee.cloud.discover.dto import ListStaffListingsRequest, StaffListingPage
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.platform import audit

router = APIRouter(prefix="/discover", tags=["platform"])

_READ = "platform.discover.read"
_MODERATE = "platform.discover.moderate"


class PlatformModerateIn(BaseModel):
    reason: str


class PlatformModerateOut(BaseModel):
    id: str
    featured: bool
    hidden: bool
    audit_event_id: str


class PlatformReindexOut(BaseModel):
    source: str
    upserted: int
    removed: int
    audit_event_id: str


def _reason(body: PlatformModerateIn) -> str:
    reason = body.reason.strip()
    if not reason:
        raise ValidationError("platform.discover.invalid_reason", "reason is required")
    return reason


@router.get("", response_model=StaffListingPage)
async def list_listings(
    request: Request,
    operator: Annotated[User, Depends(require_platform(_READ))],
    query: ListStaffListingsRequest = Depends(),
) -> dict:
    """Every listing, hidden ones included, newest first, with its moderation
    state (hidden, featured, report counts, workspace, owner, source id)."""
    page = await service_admin.list_all(query)
    await audit.record_read(
        operator=operator,
        action=_READ,
        query=" ".join(f"{k}={v!r}" for k, v in query.model_dump(exclude_none=True).items()),
        target_type="discover_index",
        request=request,
    )
    return page


async def _moderate(
    listing_id: str,
    body: PlatformModerateIn,
    request: Request,
    operator: User,
    verb: str,
) -> PlatformModerateOut:
    """Audit-wrap one feature / unfeature / hide / unhide."""
    reason = _reason(body)
    # Read first: a missing listing 404s before any audit row is written.
    before = await service_admin.get_staff(listing_id)
    event = await audit.begin(
        operator=operator,
        action=_MODERATE,
        reason=reason,
        target_type="discover_listing",
        target_workspace=before["workspace_id"],
        before={
            "listing_id": listing_id,
            "verb": verb,
            "featured": before["featured"],
            "hidden": before["hidden"],
            "report_count": before["report_count"],
        },
        request=request,
    )
    try:
        if verb in ("feature", "unfeature"):
            result: dict[str, Any] = await service_admin.set_featured(listing_id, verb == "feature")
        else:
            result = await service_admin.set_hidden(listing_id, verb == "hide")
    except CloudError:
        await audit.settle(event, ok=False, after={"listing_id": listing_id, "error": "cloud"})
        raise
    except Exception:
        await audit.settle(event, ok=False, after={"listing_id": listing_id, "error": "unexpected"})
        raise
    await audit.settle(
        event,
        ok=True,
        after={
            "listing_id": listing_id,
            "featured": result["featured"],
            "hidden": result["hidden"],
        },
    )
    return PlatformModerateOut(**result, audit_event_id=str(event.id))


@router.post("/reindex", response_model=PlatformReindexOut)
async def reindex(
    body: PlatformModerateIn,
    request: Request,
    operator: Annotated[User, Depends(require_platform(_MODERATE))],
    source: Annotated[str, Query(max_length=64)] = service_admin.SITE_TEMPLATE,
) -> PlatformReindexOut:
    """Rebuild one source's listings now (idempotent). 422
    ``discover.reindex_unsupported`` for a source that can't reindex."""
    reason = _reason(body)
    event = await audit.begin(
        operator=operator,
        action=_MODERATE,
        reason=reason,
        target_type="discover_index",
        before={"verb": "reindex", "source": source},
        request=request,
    )
    try:
        result = await service_admin.reindex(source)
    except CloudError:
        await audit.settle(event, ok=False, after={"source": source, "error": "cloud"})
        raise
    except Exception:
        await audit.settle(event, ok=False, after={"source": source, "error": "unexpected"})
        raise
    await audit.settle(event, ok=True, after=result)
    return PlatformReindexOut(**result, audit_event_id=str(event.id))


@router.post("/{listing_id}/feature", response_model=PlatformModerateOut)
async def feature(
    listing_id: str,
    body: PlatformModerateIn,
    request: Request,
    operator: Annotated[User, Depends(require_platform(_MODERATE))],
) -> PlatformModerateOut:
    """Feature a listing (hidden ones included)."""
    return await _moderate(listing_id, body, request, operator, "feature")


@router.post("/{listing_id}/unfeature", response_model=PlatformModerateOut)
async def unfeature(
    listing_id: str,
    body: PlatformModerateIn,
    request: Request,
    operator: Annotated[User, Depends(require_platform(_MODERATE))],
) -> PlatformModerateOut:
    """Unfeature a listing."""
    return await _moderate(listing_id, body, request, operator, "unfeature")


@router.post("/{listing_id}/hide", response_model=PlatformModerateOut)
async def hide(
    listing_id: str,
    body: PlatformModerateIn,
    request: Request,
    operator: Annotated[User, Depends(require_platform(_MODERATE))],
) -> PlatformModerateOut:
    """Hide a listing and its source item; reports are kept."""
    return await _moderate(listing_id, body, request, operator, "hide")


@router.post("/{listing_id}/unhide", response_model=PlatformModerateOut)
async def unhide(
    listing_id: str,
    body: PlatformModerateIn,
    request: Request,
    operator: Annotated[User, Depends(require_platform(_MODERATE))],
) -> PlatformModerateOut:
    """Unhide a listing and its source item; clears its reports and records the
    reporters so their later reports on it are ignored."""
    return await _moderate(listing_id, body, request, operator, "unhide")


__all__ = ["router"]
