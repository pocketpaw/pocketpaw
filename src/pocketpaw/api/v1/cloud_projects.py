"""Cloud projects router — create workspace-scoped project folders.

Each project is an S3 key prefix (folder) at::

    projects/{workspace_id}/{user_id}/{project_name}/

The project folder is created by writing a zero-byte marker object at
that prefix. S3-compatible stores (AWS S3, MinIO, Cloudflare R2) render
this as a folder in their UI; the local-disk adapter creates an empty
directory marker file.

Created: 2026-06-22
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from pocketpaw.uploads.factory import build_adapter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Cloud Projects"])

# ── Singleton adapter (same local root as the uploads module) ─────────────────
_ROOT = Path.home() / ".pocketpaw" / "uploads"
_ADAPTER = build_adapter(_ROOT)


class CreateCloudProjectRequest(BaseModel):
    """Request body for creating a new cloud project."""

    project_name: str


class CreateCloudProjectResponse(BaseModel):
    """Response after successfully creating a cloud project folder."""

    ok: bool
    project_name: str
    path: str
    workspace_id: str
    user_id: str


async def _empty_stream() -> AsyncIterator[bytes]:
    """An async iterator that yields nothing (zero-byte marker)."""
    if False:
        yield b""


@router.post("/cloud/projects")
async def create_cloud_project(
    req: CreateCloudProjectRequest,
    http_request: Request,
) -> CreateCloudProjectResponse:
    """Create a new project folder in cloud storage.

    The folder is an empty key prefix at::

        projects/{workspace_id}/{user_id}/{project_name}/

    ``workspace_id`` is read from the ``X-Workspace-Id`` request header
    (set automatically by the frontend's ``api.*`` client helpers).
    ``user_id`` is extracted from the authenticated request context
    (API key or OAuth token), falling back to ``"local"`` in self-hosted
    single-user mode.

    Returns 409 if a project with the same name already exists.
    """
    name = req.project_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="project_name must not be empty")
    if "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail="project_name must not contain slashes")
    if len(name) > 128:
        raise HTTPException(status_code=400, detail="project_name too long (max 128 chars)")

    # Workspace ID from the header (set by the frontend's X-Workspace-Id).
    workspace_id = http_request.headers.get("X-Workspace-Id", "default")

    # User ID — best-effort from auth state, falls back to "local" for
    # single-user self-hosted deployments that bypass real auth.
    user_id: str = "local"
    api_key = getattr(http_request.state, "api_key", None)
    if api_key is not None:
        user_id = getattr(api_key, "user_id", "local")
    oauth_token = getattr(http_request.state, "oauth_token", None)
    if oauth_token is not None:
        user_id = getattr(oauth_token, "sub", str(oauth_token.get("sub", "local")))

    # Build the storage key prefix (folder path).
    project_key = f"projects/{workspace_id}/{user_id}/{name}/"

    # Idempotency check — skip creation if the marker already exists.
    try:
        if await _ADAPTER.exists(project_key):
            raise HTTPException(
                status_code=409,
                detail=f"Project '{name}' already exists in this workspace",
            )
    except HTTPException:
        raise
    except Exception:
        # If the adapter can't check existence (e.g. transient error),
        # attempt creation anyway — the put below will surface failures.
        pass

    try:
        await _ADAPTER.put(project_key, _empty_stream(), "application/x-directory")
    except Exception as exc:
        logger.exception("Failed to create cloud project folder")
        raise HTTPException(status_code=500, detail=f"Failed to create project: {exc}") from exc

    logger.info(
        "Created cloud project folder: key=%s workspace=%s user=%s",
        project_key,
        workspace_id,
        user_id,
    )

    return CreateCloudProjectResponse(
        ok=True,
        project_name=name,
        path=project_key,
        workspace_id=workspace_id,
        user_id=user_id,
    )


@router.get("/cloud/projects")
async def list_cloud_projects(
    http_request: Request,
) -> list[dict]:
    """List all cloud projects for the current workspace + user.

    Returns basic metadata (name, path) for each project folder found
    under ``projects/{workspace_id}/{user_id}/``.

    This is a best-effort listing — the adapter must support listing
    keys with a prefix. S3 adapters do; the local adapter implements
    ``local_path()`` and we fall back to filesystem ``iterdir``.
    """
    workspace_id = http_request.headers.get("X-Workspace-Id", "default")
    user_id: str = "local"
    api_key = getattr(http_request.state, "api_key", None)
    if api_key is not None:
        user_id = getattr(api_key, "user_id", "local")
    oauth_token = getattr(http_request.state, "oauth_token", None)
    if oauth_token is not None:
        user_id = getattr(oauth_token, "sub", str(oauth_token.get("sub", "local")))

    prefix = f"projects/{workspace_id}/{user_id}/"

    projects: list[dict] = []

    # Use the adapter's unified list_prefix method. S3 adapter returns
    # CommonPrefixes (one level); local adapter returns child names.
    try:
        names = await _ADAPTER.list_prefix(prefix)
    except Exception:
        return projects

    for name in names:
        projects.append(
            {
                "project_name": name,
                "path": f"{prefix}{name}/",
            }
        )
    return projects
