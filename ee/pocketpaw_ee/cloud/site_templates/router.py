# router.py — FastAPI router for site templates (/site-templates).
#
# Thin HTTP surface over ``site_templates.service``: save a site pocket as a
# template (private, workspace or public), list by scope, get / patch / delete,
# report a public one, and start a new site pocket from one in the caller's
# workspace. Tenancy comes from the auth context, never the body. No
# Beanie doc import here (import-linter "SiteTemplates" contract); errors are
# CloudError subclasses mapped by the global handler. Mounted under /api/v1
# from ``ee/pocketpaw_ee/cloud/__init__.py``.

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.deps import current_user_id, current_workspace_id
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.site_templates import service as site_templates_service
from pocketpaw_ee.cloud.site_templates.dto import (
    ListSiteTemplatesRequest,
    PatchSiteTemplateRequest,
    ReportSiteTemplateRequest,
    SaveSiteTemplateRequest,
    SiteTemplateListResponse,
    SiteTemplateResponse,
    UseSiteTemplateRequest,
    UseSiteTemplateResponse,
)

router = APIRouter(
    prefix="/site-templates",
    tags=["Site templates"],
    dependencies=[Depends(require_license)],
)


@router.post("", response_model=SiteTemplateResponse)
async def save_template(
    body: SaveSiteTemplateRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Save a site pocket as a template the caller owns (private by default)."""
    return await site_templates_service.save_template(workspace_id, user_id, body)


@router.get("", response_model=SiteTemplateListResponse)
async def list_templates(
    scope: Literal["mine", "workspace", "public"] = Query(default="mine"),
    limit: int = Query(default=50, ge=1, le=50),
    cursor: str | None = Query(default=None),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """A page of templates, newest first: your own (``mine``), shared with this
    workspace (``workspace``) or shared with everyone (``public``)."""
    body = ListSiteTemplatesRequest(scope=scope, limit=limit, cursor=cursor)
    return await site_templates_service.list_templates(workspace_id, user_id, body)


@router.get("/{template_id}", response_model=SiteTemplateResponse)
async def get_template(
    template_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    return await site_templates_service.get_template(workspace_id, user_id, template_id)


@router.patch("/{template_id}", response_model=SiteTemplateResponse)
async def update_template(
    template_id: str,
    body: PatchSiteTemplateRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Rename, redescribe or change the visibility of a template you own."""
    return await site_templates_service.update_template(workspace_id, user_id, template_id, body)


@router.post("/{template_id}/report")
async def report_template(
    template_id: str,
    body: ReportSiteTemplateRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Report a public template. One report per user; a repeat is a no-op."""
    return await site_templates_service.report_template(workspace_id, user_id, template_id, body)


@router.delete("/{template_id}")
async def delete_template(
    template_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Delete a template the caller owns. Pockets made from it are untouched."""
    return await site_templates_service.delete_template(workspace_id, user_id, template_id)


@router.post("/{template_id}/use", response_model=UseSiteTemplateResponse)
async def use_template(
    template_id: str,
    body: UseSiteTemplateRequest | None = None,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Start a new private site pocket in your workspace from a template you can
    see. Returns its ``pocket_id``."""
    return await site_templates_service.use_template(
        workspace_id, user_id, template_id, body or UseSiteTemplateRequest()
    )
