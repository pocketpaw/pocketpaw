# router.py — FastAPI router for studio templates (/studio-templates).
#
# Created 2026-10-02 (feat/studio-templates). Thin HTTP surface over
# ``studio_templates.service``: publish a Studio generation as a template
# (private, workspace or public; public lists it on Discover), list your own,
# patch and delete. Tenancy comes from the auth context, never the body. No
# Beanie doc import here (import-linter "StudioTemplates" contract). Mounted
# under /api/v1 from ``ee/pocketpaw_ee/cloud/__init__.py``.

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from pocketpaw_ee.cloud._core.deps import current_user_id, current_workspace_id
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.studio_templates import service as studio_templates_service
from pocketpaw_ee.cloud.studio_templates.dto import (
    ListStudioTemplatesRequest,
    PatchStudioTemplateRequest,
    PublishStudioTemplateRequest,
    StudioTemplateListResponse,
    StudioTemplateResponse,
)

router = APIRouter(
    prefix="/studio-templates",
    tags=["Studio templates"],
    dependencies=[Depends(require_license)],
)


@router.post("", response_model=StudioTemplateResponse)
async def publish_template(
    body: PublishStudioTemplateRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Publish a finished Studio generation as a template you own."""
    return await studio_templates_service.publish_template(workspace_id, user_id, body)


@router.get("", response_model=StudioTemplateListResponse)
async def list_templates(
    limit: int = Query(default=50, ge=1, le=50),
    cursor: str | None = Query(default=None),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Your own templates in this workspace, newest first."""
    body = ListStudioTemplatesRequest(limit=limit, cursor=cursor)
    return await studio_templates_service.list_templates(workspace_id, user_id, body)


@router.patch("/{template_id}", response_model=StudioTemplateResponse)
async def update_template(
    template_id: str,
    body: PatchStudioTemplateRequest,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Retitle, redescribe or change the visibility of a template you own."""
    return await studio_templates_service.update_template(workspace_id, user_id, template_id, body)


@router.delete("/{template_id}")
async def delete_template(
    template_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> dict:
    """Delete a template you own. The source generation is untouched."""
    return await studio_templates_service.delete_template(workspace_id, user_id, template_id)
