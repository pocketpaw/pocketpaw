# router.py — FastAPI router for the file-version write API.
# Created: 2026-06-26 (ART-1) — ported from dewani12's origin/feature/files
#   (ee/cloud/file_versions/router.py). KEEPS the 4 core routes: POST
#   /files/write, PUT /files/{id}, GET /files/{id}/versions,
#   GET /files/{id}/versions/{vid}. DROPPED the Slice-D editor routes
#   (revert, diff, ai-edit, editing/spreadsheet/slides-context). Thin
#   pass-through: ``Depends(request_context)``, no Beanie, no HTTPException —
#   the service raises CloudError subclasses and ``_core.http`` maps them to
#   JSON. Coexists with the existing /files router (GET /files, /tree,
#   /browse) — no method+path collisions.
# Updated: 2026-07-03 (FL-2, port of #1193) — restored the two history routes
#   ART-1 deferred: POST /files/{id}/versions/{vid}/revert (restore a prior
#   version) and GET /files/{id}/versions/{v1}/diff/{v2} (unified diff). The
#   AI-edit / editing-context routes remain deferred (they pull in FL-5 tool
#   deps). Doc note: a stale If-Match now maps to 412 (PreconditionFailed),
#   not 409.
"""FileVersions router — file-version write + history API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import BadRequest
from pocketpaw_ee.cloud.file_versions import service as _svc
from pocketpaw_ee.cloud.file_versions.dto import (
    DiffResponse,
    FileVersionListItem,
    FileVersionResponse,
    UpdateFileContentRequest,
    UpdateFileContentResponse,
    WriteFileRequest,
    WriteFileResponse,
)
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(
    prefix="/files",
    tags=["File Versions"],
    dependencies=[Depends(require_license)],
)


@router.post("/write", status_code=201, response_model=WriteFileResponse)
async def write_file(
    body: WriteFileRequest,
    ctx: RequestContext = Depends(request_context),
) -> WriteFileResponse:
    """Create or overwrite a file. Returns the file id for subsequent versioned saves."""
    return await _svc.write_file(ctx, body)


@router.put("/{file_id}", response_model=UpdateFileContentResponse)
async def update_file(
    file_id: str,
    body: UpdateFileContentRequest,
    ctx: RequestContext = Depends(request_context),
    if_match: str | None = Header(None, alias="If-Match"),
) -> UpdateFileContentResponse:
    """Replace a text file's content inline.

    Body: ``{ content: "<new text>", expectedVersion?: <int> }``.
    Header: ``If-Match: <current content_version>`` (optimistic concurrency)
    takes precedence over ``expectedVersion`` when present.

    Raises (mapped to JSON by ``_core.http``): 404 if the file is missing,
    412 on a stale ``If-Match`` (version conflict), 422 if the file type isn't
    editable.
    """
    # If-Match header (optimistic concurrency) wins over the body field.
    if if_match is not None:
        try:
            body.expected_version = int(if_match.strip('"'))
        except ValueError:
            raise BadRequest("files.bad_version", "If-Match must be an integer.") from None

    return await _svc.update_file_content(ctx, file_id, body)


@router.get("/{file_id}/versions", response_model=list[FileVersionListItem])
async def list_file_versions(
    file_id: str,
    ctx: RequestContext = Depends(request_context),
) -> list[FileVersionListItem]:
    """List all archived versions for a file (oldest first, no content)."""
    return await _svc.list_versions(ctx, file_id)


@router.get("/{file_id}/versions/{version_id}", response_model=FileVersionResponse)
async def get_file_version(
    file_id: str,
    version_id: str,
    ctx: RequestContext = Depends(request_context),
) -> FileVersionResponse:
    """Fetch a single version with full content (for revert preview / diff)."""
    return await _svc.get_version(ctx, file_id, version_id)


@router.post(
    "/{file_id}/versions/{version_id}/revert",
    response_model=UpdateFileContentResponse,
)
async def revert_file_version(
    file_id: str,
    version_id: str,
    ctx: RequestContext = Depends(request_context),
) -> UpdateFileContentResponse:
    """Restore the live file to a historical version.

    Archives the current content as a new version, then writes the target
    version's content as the new live content. Tenant-filtered: a
    cross-workspace ``version_id`` is a 404.
    """
    return await _svc.revert_to_version(ctx, file_id, version_id)


@router.get(
    "/{file_id}/versions/{from_version_id}/diff/{to_version_id}",
    response_model=DiffResponse,
)
async def diff_file_versions(
    file_id: str,
    from_version_id: str,
    to_version_id: str,
    ctx: RequestContext = Depends(request_context),
) -> DiffResponse:
    """Return a unified diff between two archived versions (``from`` -> ``to``).

    Tenant-filtered: both versions are fetched workspace-scoped, so a diff can
    never span a workspace boundary (a cross-workspace id is a 404).
    """
    return await _svc.diff_versions(ctx, file_id, from_version_id, to_version_id)
