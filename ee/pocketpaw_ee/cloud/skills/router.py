# router.py — FastAPI router for the cloud Skills entity.
# Created: 2026-05-22 (feat/api-skills, Increment 2b) — exposes
# POST /api/v1/skills/api-doc, a multipart upload that installs a
# backend's OpenAPI / Swagger document as a per-backend API skill.
# Thin router: validates the upload extension + size, reads the bytes,
# and delegates to skills.service.install_api_doc. Never raises
# HTTPException for domain failures — CloudError maps to JSON via
# _core.http. Mounted in mount_cloud() alongside the other domain
# routers. Guarded by skills.manage (ADMIN).
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.shared.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)
from pocketpaw_ee.cloud.skills import service as skills_service
from pocketpaw_ee.cloud.skills.domain import ApiDocInstall
from pocketpaw_ee.cloud.skills.dto import InstallApiDocResponse

# A spec upload bigger than this is rejected before the bytes are read
# into memory — mirrors the 2 MB cap in skills.service / api_skill_builder.
_MAX_SPEC_BYTES = 2 * 1024 * 1024
_ALLOWED_EXTENSIONS = (".json", ".yaml", ".yml")

router = APIRouter(
    prefix="/skills",
    tags=["Skills"],
    dependencies=[Depends(require_license)],
)


@router.post(
    "/api-doc",
    response_model=InstallApiDocResponse,
    dependencies=[Depends(require_action_any_workspace("skills.manage"))],
)
async def install_api_doc(
    file: Annotated[UploadFile, File(...)],
    name: Annotated[str | None, Form()] = None,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
) -> InstallApiDocResponse:
    """Install an uploaded OpenAPI / Swagger document as an API skill.

    Accepts a multipart upload — ``file`` (the spec, ``.json`` /
    ``.yaml`` / ``.yml``) and an optional ``name`` (the backend display
    name). The resulting skill lives under ``~/.pocketpaw/skills/`` so
    the pocket-authoring agent can load the backend's real endpoints.
    Requires the ``skills.manage`` role (ADMIN).
    """
    filename = file.filename or ""
    if not filename.lower().endswith(_ALLOWED_EXTENSIONS):
        raise ValidationError(
            "skills.api_doc.bad_extension",
            f"spec file must be one of {', '.join(_ALLOWED_EXTENSIONS)}",
        )

    spec_bytes = await file.read()
    if len(spec_bytes) > _MAX_SPEC_BYTES:
        raise ValidationError(
            "skills.api_doc.too_large",
            f"spec file is {len(spec_bytes)} bytes — exceeds the 2 MB limit",
        )

    body = ApiDocInstall(
        workspace_id=workspace_id,
        user_id=user_id,
        filename=filename,
        spec_bytes=spec_bytes,
        name=name,
    )
    return await skills_service.install_api_doc(workspace_id, user_id, body)
