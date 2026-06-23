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

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from pocketpaw.api.v1.schemas.files import (
    BrowseResponse,
    CreateFileRequest,
    FileActionResponse,
    FileEntry,
    MkdirRequest,
    RenameRequest,
    WriteFileRequest,
)
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
    local_path: str
    workspace_id: str
    user_id: str


async def _empty_stream() -> AsyncIterator[bytes]:
    """An async iterator that yields nothing (zero-byte marker)."""
    if False:
        yield b""


def _resolve_ids(http_request: Request) -> tuple[str, str]:
    """Extract workspace_id and user_id from the request context.

    ``workspace_id`` is read from the ``X-Workspace-Id`` request header.
    ``user_id`` is extracted from the authenticated request context
    (API key or OAuth token), falling back to ``"local"`` in self-hosted
    single-user mode.
    """
    workspace_id = http_request.headers.get("X-Workspace-Id", "default")
    user_id: str = "local"
    api_key = getattr(http_request.state, "api_key", None)
    if api_key is not None:
        user_id = getattr(api_key, "user_id", "local")
    oauth_token = getattr(http_request.state, "oauth_token", None)
    if oauth_token is not None:
        user_id = getattr(oauth_token, "sub", str(oauth_token.get("sub", "local")))
    return workspace_id, user_id


async def _require_project(workspace_id: str, user_id: str, project_name: str) -> str:
    """Return the project key prefix. Raises 404 if the project does not exist."""
    project_key = f"projects/{workspace_id}/{user_id}/{project_name}/"
    try:
        if not await _ADAPTER.exists(project_key):
            raise HTTPException(
                status_code=404,
                detail=f"Cloud project '{project_name}' not found",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not verify project existence: {exc}",
        ) from exc
    return project_key


async def _project_filesystem_path(workspace_id: str, user_id: str, project_name: str) -> str:
    """Return the local filesystem path for a project, or empty string for
    remote-only adapters (S3)."""
    project_key = f"projects/{workspace_id}/{user_id}/{project_name}/"
    raw_local = _ADAPTER.local_path(project_key)
    if raw_local is not None:
        return f"~/{raw_local.relative_to(Path.home()).as_posix()}"
    return ""


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

    workspace_id, user_id = _resolve_ids(http_request)
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

    filesystem_path = await _project_filesystem_path(workspace_id, user_id, name)

    return CreateCloudProjectResponse(
        ok=True,
        project_name=name,
        path=project_key,
        local_path=filesystem_path,
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
    workspace_id, user_id = _resolve_ids(http_request)
    prefix = f"projects/{workspace_id}/{user_id}/"

    projects: list[dict] = []

    try:
        names = await _ADAPTER.list_prefix(prefix)
    except Exception:
        return projects

    for name in names:
        filesystem_path = await _project_filesystem_path(workspace_id, user_id, name)
        projects.append(
            {
                "project_name": name,
                "path": f"{prefix}{name}/",
                "local_path": filesystem_path,
            }
        )
    return projects


# ── Cloud project file operations ────────────────────────────────────────────
# These endpoints proxy file reads/writes/listing through the StorageAdapter so
# that cloud project files work regardless of the storage backend (local disk,
# S3, MinIO, R2, etc.). The frontend routes to these when the workspace is
# scoped to a cloud project.
#
# Each endpoint:
#   1. Resolves workspace_id + user_id from the request
#   2. Validates the named project exists
#   3. Translates the relative file path to an adapter key
#   4. Delegates to the StorageAdapter

_MAX_VIEWABLE_BYTES = 50 * 1024 * 1024  # 50 MB


def _adapter_key(project_key: str, relative_path: str) -> str:
    """Build the full adapter key for a file within a project.

    ``relative_path`` may be empty (root), ``"."`` (root), a filename, or
    a ``/``-delimited subpath. Leading slashes are stripped.
    """
    clean = relative_path.strip("/")
    if not clean or clean == ".":
        return project_key
    return f"{project_key}{clean}"


@router.get("/cloud/projects/{project_name}/files/browse")
async def browse_cloud_project_files(
    project_name: str,
    path: str = Query("", description="Relative path within the project"),
    http_request: Request = None,
) -> BrowseResponse:
    """List the contents of a directory within a cloud project.

    ``path`` is relative to the project root. Empty string or ``"."``
    lists the project root.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)
    key = _adapter_key(project_key, path)

    try:
        items = await _ADAPTER.browse(key)
    except Exception as exc:
        logger.warning("cloud browse failed: key=%s  err=%s", key, exc)
        return BrowseResponse(path=path, error="Could not list directory")

    files = [
        FileEntry(
            name=item.name,
            isDir=item.is_dir,
            size=str(item.size) if not item.is_dir and item.size > 0 else "",
        )
        for item in items
    ]
    return BrowseResponse(path=path, files=files)


@router.get("/cloud/projects/{project_name}/files/content")
async def read_cloud_project_file(
    project_name: str,
    path: str = Query(..., description="Relative file path within the project"),
    http_request: Request = None,
):
    """Read a file from a cloud project and return its raw content.

    The path is relative to the project root. The response uses the
    file's MIME type for syntax highlighting in the browser.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    if not path or path.strip("/") == "":
        raise HTTPException(status_code=400, detail="Cannot read a directory")

    key = _adapter_key(project_key, path)

    # Stream the content through the adapter.
    try:
        chunks: list[bytes] = []
        async for chunk in _ADAPTER.open(key):
            chunks.append(chunk)
    except Exception as exc:
        from pocketpaw.uploads.errors import NotFound as AdapterNotFound

        if isinstance(exc, AdapterNotFound):
            raise HTTPException(status_code=404, detail="File not found") from exc
        logger.warning("cloud read failed: key=%s  err=%s", key, exc)
        raise HTTPException(status_code=500, detail="Could not read file") from exc

    body = b"".join(chunks)
    if len(body) > _MAX_VIEWABLE_BYTES:
        raise HTTPException(status_code=413, detail="File too large to view (max 50 MB)")

    # Guess MIME type from the file name
    import mimetypes

    mime, _ = mimetypes.guess_type(path)
    return Response(content=body, media_type=mime or "application/octet-stream")


@router.post("/cloud/projects/{project_name}/files/write")
async def write_cloud_project_file(
    project_name: str,
    req: WriteFileRequest,
    http_request: Request = None,
) -> FileActionResponse:
    """Create or overwrite a file within a cloud project.

    The body ``path`` is relative to the project root. Parent directories
    are created automatically.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    relative = req.path.strip("/")
    if not relative:
        raise HTTPException(status_code=400, detail="path must not be empty")

    key = _adapter_key(project_key, relative)

    try:
        content_bytes = req.content.encode("utf-8")

        async def _single_chunk() -> AsyncIterator[bytes]:
            yield content_bytes

        await _ADAPTER.put(key, _single_chunk(), "text/plain")
    except Exception as exc:
        logger.warning("cloud write failed: key=%s  err=%s", key, exc)
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}") from exc

    return FileActionResponse(ok=True, path=relative)


@router.post("/cloud/projects/{project_name}/files/create")
async def create_cloud_project_file(
    project_name: str,
    req: CreateFileRequest,
    http_request: Request = None,
) -> FileActionResponse:
    """Create a new file within a cloud project.

    Returns 409 if the file already exists.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    relative = req.path.strip("/")
    if not relative:
        raise HTTPException(status_code=400, detail="path must not be empty")

    key = _adapter_key(project_key, relative)

    # Check for existing file.
    try:
        if await _ADAPTER.exists(key):
            raise HTTPException(
                status_code=409,
                detail=f"File already exists: {relative}",
            )
    except HTTPException:
        raise
    except Exception:
        pass

    try:
        content_bytes = req.content.encode("utf-8")

        async def _file_chunk() -> AsyncIterator[bytes]:
            yield content_bytes

        await _ADAPTER.put(key, _file_chunk(), "text/plain")
    except Exception as exc:
        logger.warning("cloud create failed: key=%s  err=%s", key, exc)
        raise HTTPException(status_code=500, detail=f"Create failed: {exc}") from exc

    return FileActionResponse(ok=True, path=relative)


@router.post("/cloud/projects/{project_name}/files/mkdir")
async def mkdir_cloud_project(
    project_name: str,
    req: MkdirRequest,
    http_request: Request = None,
) -> FileActionResponse:
    """Create a directory within a cloud project.

    The directory is a zero-byte marker object at the corresponding key
    prefix. Returns 409 if the directory already exists.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    relative = req.path.strip("/")
    if not relative:
        raise HTTPException(status_code=400, detail="path must not be empty")

    key = _adapter_key(project_key, relative) + "/"

    try:
        if await _ADAPTER.exists(key):
            raise HTTPException(
                status_code=409,
                detail=f"Directory already exists: {relative}",
            )
    except HTTPException:
        raise
    except Exception:
        pass

    try:
        await _ADAPTER.put(key, _empty_stream(), "application/x-directory")
    except Exception as exc:
        logger.warning("cloud mkdir failed: key=%s  err=%s", key, exc)
        raise HTTPException(status_code=500, detail=f"Mkdir failed: {exc}") from exc

    return FileActionResponse(ok=True, path=relative)


@router.patch("/cloud/projects/{project_name}/files/rename")
async def rename_cloud_project_item(
    project_name: str,
    req: RenameRequest,
    http_request: Request = None,
) -> FileActionResponse:
    """Rename or move a file/directory within a cloud project.

    Both ``path`` and ``new_path`` are relative to the project root.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    old_relative = req.path.strip("/")
    new_relative = req.new_path.strip("/")
    if not old_relative:
        raise HTTPException(status_code=400, detail="path must not be empty")
    if not new_relative:
        raise HTTPException(status_code=400, detail="new_path must not be empty")

    old_key = _adapter_key(project_key, old_relative)
    new_key = _adapter_key(project_key, new_relative)

    try:
        await _ADAPTER.rename_key(old_key, new_key)
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="Rename not supported by storage backend")
    except Exception as exc:
        logger.warning("cloud rename failed: old=%s new=%s  err=%s", old_key, new_key, exc)
        raise HTTPException(status_code=500, detail=f"Rename failed: {exc}") from exc

    return FileActionResponse(ok=True, path=new_relative)


@router.delete("/cloud/projects/{project_name}/files/delete")
async def delete_cloud_project_item(
    project_name: str,
    path: str = Query(..., description="Relative path to delete"),
    http_request: Request = None,
) -> FileActionResponse:
    """Delete a file or empty directory within a cloud project."""
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    relative = path.strip("/")
    if not relative:
        raise HTTPException(status_code=400, detail="path must not be empty")

    key = _adapter_key(project_key, relative)

    try:
        await _ADAPTER.delete(key)
    except Exception as exc:
        logger.warning("cloud delete failed: key=%s  err=%s", key, exc)
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}") from exc

    return FileActionResponse(ok=True)
