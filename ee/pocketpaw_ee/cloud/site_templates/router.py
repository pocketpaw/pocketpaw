# router.py — FastAPI router for site templates (/site-templates).
#
# Thin HTTP surface over ``site_templates.service``: save a site pocket as a
# private template, list / get / delete your templates, and start a new site
# pocket from one. Tenancy comes from the auth context, never the body. No
# Beanie doc import here (import-linter "SiteTemplates" contract); errors are
# CloudError subclasses mapped by the global handler. Mounted under /api/v1
# from ``ee/pocketpaw_ee/cloud/__init__.py``.

from __future__ import annotations

from fastapi import APIRouter, Depends

from pocketpaw_ee.cloud._core.deps import current_user_id, current_workspace_id
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.site_templates import service as site_templates_service
from pocketpaw_ee.cloud.site_templates.dto import (
    SaveSiteTemplateRequest,
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
    """Save a site pocket as a private template the caller owns."""
    return await site_templates_service.save_template(workspace_id, user_id, body)


@router.get("", response_model=list[SiteTemplateResponse])
async def list_templates(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> list[dict]:
    """The caller's own templates in this workspace, newest first."""
    return await site_templates_service.list_templates(workspace_id, user_id)


@router.get("/{template_id}", response_model=SiteTemplateResponse)
async def get_template(
    template_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    return await site_templates_service.get_template(workspace_id, user_id, template_id)


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
    """Start a new private site pocket from a template. Returns its ``pocket_id``."""
    return await site_templates_service.use_template(
        workspace_id, user_id, template_id, body or UseSiteTemplateRequest()
    )
