"""Daytona workspace router — manage Daytona sandboxes for cloud projects.

These endpoints are mounted at ``/api/v1/`` (alongside the other v1 routers)
and provide a project-scoped API for provisioning, syncing, and managing
Daytona sandboxes (VMs/containers) that back cloud project execution.

Every endpoint requires a cloud project to exist first (created via
``POST /api/v1/cloud/projects`` or ``POST /api/v1/cloud/projects/clone``).
The Daytona sandbox is an optional compute layer on top of the S3-backed
project storage.

Endpoints:

  POST   /cloud/projects/{name}/workspace                  — Provision Daytona sandbox
  GET    /cloud/projects/{name}/workspace                   — Get sandbox status
  DELETE /cloud/projects/{name}/workspace                   — Destroy sandbox
  POST   /cloud/projects/{name}/workspace/sync-to-sandbox   — S3 → Sandbox
  POST   /cloud/projects/{name}/workspace/sync-to-s3        — Sandbox → S3
  POST   /cloud/projects/{name}/workspace/sync-and-finish   — Sandbox → S3 → stop sandbox
  GET    /cloud/projects/{name}/workspace/terminal           — Get web terminal URL

Key integration points:
  • ``pocketpaw_ee.cloud.daytona.client.DaytonaClient`` — wraps the official ``daytona`` SDK
  • The storage adapter (S3/local) for project file access --
    from OSS ``pocketpaw.api.v1.cloud_projects``
  • The project key pattern: ``projects/{workspace_id}/{user_id}/{name}/``

Moved from OSS to EE: 2026-06-24
Updated: 2026-07-01 — uses shared store module; added sync-and-finish endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from pocketpaw.api.v1.cloud_projects import _ADAPTER, _require_project, _resolve_ids
from pocketpaw.api.v1.schemas.files import (
    BrowseResponse,
    FileActionResponse,
    FileEntry,
    MkdirRequest,
    RenameRequest,
    WriteFileRequest,
)
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.daytona.config import daytona_enabled
from pocketpaw_ee.cloud.daytona.store import (
    get_sandbox_id,
    remove_sandbox_id,
    set_sandbox_id,
    update_sync_timestamp,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Daytona Workspaces"])

# ── Helpers ────────────────────────────────────────────────────────────────


async def _require_daytona() -> DaytonaClient:
    """Return the Daytona client or raise 501 if not configured."""
    if not daytona_enabled():
        raise HTTPException(
            status_code=501,
            detail="Daytona is not configured. Set DAYTONA_API_URL and DAYTONA_API_KEY.",
        )
    client = get_daytona_client()
    if client is None:
        raise HTTPException(status_code=501, detail="Daytona client is not available")
    return client


# ── Storage adapter walking ──────────────────────────────────────────────


async def _walk_adapter_files(prefix: str) -> list[tuple[str, str]]:
    """Recursively walk the storage adapter tree under *prefix*.

    Returns ``(full_key, relative_path)`` tuples for every file found.
    """
    files: list[tuple[str, str]] = []
    stack = [("", prefix)]

    while stack:
        rel_dir, dir_key = stack.pop()
        try:
            items = await _ADAPTER.browse(dir_key)
        except Exception:
            continue

        for item in items:
            rel_path = f"{rel_dir}/{item.name}" if rel_dir else item.name
            if item.is_dir:
                stack.append((rel_path, f"{dir_key}{item.name}/"))
            else:
                files.append((f"{dir_key}{item.name}", rel_path))

    return files


# ── Sync: S3 → Sandbox ──────────────────────────────────────────────────


async def _sync_directory_from_s3_to_sandbox(
    client: DaytonaClient,
    sandbox_id: str,
    project_key: str,
    sandbox_dir: str,
) -> None:
    """Sync all files from S3 (storage adapter) into the sandbox.

    Uses the SDK's ``upload_bytes`` for each file — no temp directory or
    tar archive needed.
    """
    logger.info(
        "Syncing files from S3 to sandbox: project=%s sandbox=%s sandbox_dir=%s",
        project_key,
        sandbox_id,
        sandbox_dir,
    )

    files = await _walk_adapter_files(project_key)
    if not files:
        logger.info("No files to sync for project %s", project_key)
        return

    uploaded = 0
    for full_key, rel_path in files:
        remote_path = f"{sandbox_dir}/{rel_path}"

        try:
            # Read file content from adapter (streaming).
            chunks: list[bytes] = []
            async for chunk in _ADAPTER.open(full_key):
                chunks.append(chunk)
            content = b"".join(chunks)

            if not content:
                continue

            # Create parent directory in sandbox.
            parent = os.path.dirname(remote_path)
            try:
                await client.create_folder(sandbox_id, parent)
            except Exception:
                pass

            # Upload via SDK — handles HTTP/streaming internally.
            await client.upload_bytes(sandbox_id, content, remote_path)
            uploaded += 1

        except Exception as exc:
            logger.warning("Failed to sync file %s to sandbox: %s", rel_path, exc)

    logger.info("Synced %d files from S3 to sandbox %s", uploaded, sandbox_id)


# ── Sync: Sandbox → S3 ──────────────────────────────────────────────────


async def _sync_directory_from_sandbox_to_s3(
    client: DaytonaClient,
    sandbox_id: str,
    project_key: str,
    sandbox_dir: str,
) -> None:
    """Sync all files from the sandbox back to S3 (storage adapter).

    Walks the sandbox filesystem using the SDK's ``list_files``, downloads
    each file via ``download_file``, and uploads to the adapter.
    """
    logger.info(
        "Syncing files from sandbox to S3: sandbox=%s sandbox_dir=%s project=%s",
        sandbox_id,
        sandbox_dir,
        project_key,
    )

    # Recursively list all files in the sandbox directory.
    files_to_download: list[str] = []
    stack = [sandbox_dir]
    visited_dirs: set[str] = set()

    while stack:
        current_dir = stack.pop()

        # Guard against re-visiting the same directory (prevents infinite
        # loops from `.` / `..` entries or symlink cycles).
        if current_dir in visited_dirs:
            continue
        visited_dirs.add(current_dir)

        try:
            entries = await client.list_files(sandbox_id, current_dir)
        except Exception as exc:
            logger.warning("Failed to list sandbox dir %s: %s", current_dir, exc)
            continue

        for entry in entries:
            name = entry.name
            # Skip self / parent directory entries that some filesystems
            # include in listings — they would re-add the current dir to
            # the stack and cause an infinite loop.
            if name in (".", ".."):
                continue

            entry_path = f"{current_dir}/{name}"

            if entry.is_dir:
                stack.append(entry_path)
            else:
                files_to_download.append(entry_path)

    # Download each file and upload to S3.
    uploaded = 0
    for remote_path in files_to_download:
        # Compute relative path within the project.
        if remote_path.startswith(sandbox_dir):
            relative = remote_path[len(sandbox_dir) :].lstrip("/")
        else:
            relative = remote_path
        storage_key = f"{project_key}{relative}"

        try:
            # Download via SDK (returns bytes).
            data = await client.download_file(sandbox_id, remote_path)

            # Upload to adapter.
            async def _stream_file(content: bytes = data):
                yield content

            import mimetypes

            mime, _ = mimetypes.guess_type(relative)
            await _ADAPTER.put(storage_key, _stream_file(data), mime or "application/octet-stream")
            uploaded += 1

        except Exception as exc:
            logger.warning("Failed to sync file %s from sandbox: %s", remote_path, exc)

    logger.info("Synced %d files from sandbox %s to S3", uploaded, sandbox_id)


# ── Sandbox state helpers ────────────────────────────────────────────────


def _sandbox_status_detail(state: str) -> str:
    """Return a human-readable status detail for a sandbox state."""
    details = {
        "creating": "Provisioning the workspace VM...",
        "started": "Running",
        "stopped": "Stopped",
        "starting": "Starting...",
        "stopping": "Stopping...",
        "error": "Error — check logs",
        "build_failed": "Build failed",
        "destroyed": "Destroyed",
        "destroying": "Destroying...",
        "archived": "Archived",
        "paused": "Paused",
        "unknown": "Unknown",
    }
    return details.get(state, f"Unknown state: {state}")


# ── Request/Response models ──────────────────────────────────────────────


class ProvisionWorkspaceResponse(BaseModel):
    ok: bool
    project_name: str
    sandbox_id: str
    sandbox_name: str
    state: str
    state_detail: str
    toolbox_url: str = ""
    project_dir: str = ""


class WorkspaceStatusResponse(BaseModel):
    ok: bool
    project_name: str
    has_workspace: bool
    sandbox_id: str = ""
    sandbox_name: str = ""
    state: str = ""
    state_detail: str = ""
    toolbox_url: str = ""
    project_dir: str = ""


class WorkspaceTerminalResponse(BaseModel):
    ok: bool
    web_terminal_url: str = ""
    sandbox_id: str = ""


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/cloud/projects/{project_name}/workspace")
async def provision_workspace(
    project_name: str,
    http_request: Request,
) -> ProvisionWorkspaceResponse:
    """Provision a Daytona sandbox for a cloud project.

    Creates a new sandbox (VM/container) for the given project. Files from
    the project's S3 storage are synced into the sandbox after provisioning.

    Returns immediately with the sandbox ID. The sandbox takes ~30-60 seconds
    to provision — poll ``GET .../workspace`` until ``state == "started"``.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    client = await _require_daytona()

    # Idempotency: check if a sandbox already exists for this project.
    existing_id = get_sandbox_id(project_key)
    if existing_id:
        try:
            existing = await client.get_sandbox_by_id(existing_id)
            if existing.state not in ("destroyed", "error"):
                # Sandbox already exists — return it.
                project_dir = ""
                try:
                    project_dir = await client.get_project_dir(existing.id)
                except Exception:
                    pass
                return ProvisionWorkspaceResponse(
                    ok=True,
                    project_name=project_name,
                    sandbox_id=existing.id,
                    sandbox_name=existing.name,
                    state=existing.state,
                    state_detail=_sandbox_status_detail(existing.state),
                    project_dir=project_dir,
                )
        except Exception:
            # Sandbox might have been deleted externally — clean up.
            remove_sandbox_id(project_key)

    # Create a new sandbox.
    sandbox_name = f"paw-{project_name}-{workspace_id[:8]}"
    try:
        info = await client.create_sandbox(
            name=sandbox_name,
            cpu=2,
            memory=4,
            disk=10,
        )
    except Exception as exc:
        logger.exception("Failed to create Daytona sandbox")
        raise HTTPException(status_code=502, detail=f"Failed to create workspace: {exc}")

    # Record the mapping.
    set_sandbox_id(project_key, info.id, sandbox_name)

    # Kick off background provisioning + sync.
    asyncio.create_task(_provision_and_sync(client, info.id, project_key))

    return ProvisionWorkspaceResponse(
        ok=True,
        project_name=project_name,
        sandbox_id=info.id,
        sandbox_name=sandbox_name,
        state=info.state,
        state_detail=_sandbox_status_detail(info.state),
        toolbox_url="",
        project_dir="",
    )


async def _provision_and_sync(client: DaytonaClient, sandbox_id: str, project_key: str) -> None:
    """Wait for sandbox to start, then sync files from S3."""
    try:
        await client.wait_for_sandbox(sandbox_id, target_state="started", timeout=120)

        # Get project directory from sandbox.
        project_dir = await client.get_project_dir(sandbox_id)

        # Sync files from S3 to sandbox.
        await _sync_directory_from_s3_to_sandbox(
            client,
            sandbox_id,
            project_key,
            project_dir,
        )
        logger.info(
            "Provisioning complete for sandbox %s on project %s",
            sandbox_id,
            project_key,
        )
    except Exception as exc:
        logger.exception(
            "Failed to provision sandbox %s for project %s: %s",
            sandbox_id,
            project_key,
            exc,
        )


@router.get("/cloud/projects/{project_name}/workspace")
async def get_workspace_status(
    project_name: str,
    http_request: Request,
) -> WorkspaceStatusResponse:
    """Get the Daytona sandbox status for a cloud project.

    Returns ``has_workspace: false`` if no sandbox has been provisioned.
    When a sandbox exists, returns its state, toolbox URL, and project dir.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    sandbox_id = get_sandbox_id(project_key)
    if not sandbox_id:
        return WorkspaceStatusResponse(
            ok=True,
            project_name=project_name,
            has_workspace=False,
        )

    if not daytona_enabled():
        return WorkspaceStatusResponse(
            ok=True,
            project_name=project_name,
            has_workspace=True,
            sandbox_id=sandbox_id,
            state="config_error",
            state_detail="Daytona API is not configured",
        )

    try:
        client = get_daytona_client()
        if client is None:
            raise HTTPException(status_code=501, detail="Daytona not available")
        info = await client.get_sandbox_by_id(sandbox_id)

        project_dir = ""
        if info.state == "started":
            try:
                project_dir = await client.get_project_dir(info.id)
            except Exception:
                pass

        return WorkspaceStatusResponse(
            ok=True,
            project_name=project_name,
            has_workspace=True,
            sandbox_id=info.id,
            sandbox_name=info.name,
            state=info.state,
            state_detail=_sandbox_status_detail(info.state),
            toolbox_url="",
            project_dir=project_dir,
        )
    except Exception:
        # Sandbox might have been deleted externally.
        remove_sandbox_id(project_key)
        return WorkspaceStatusResponse(
            ok=True,
            project_name=project_name,
            has_workspace=False,
        )


@router.delete("/cloud/projects/{project_name}/workspace")
async def delete_workspace(
    project_name: str,
    http_request: Request,
) -> dict:
    """Delete the Daytona sandbox for a cloud project.

    The project files in S3 are preserved. Only the compute sandbox is destroyed.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    sandbox_id = get_sandbox_id(project_key)
    if not sandbox_id:
        return {"ok": True, "message": "No workspace to delete"}

    try:
        client = await _require_daytona()
        await client.delete_sandbox(sandbox_id)
    except Exception as exc:
        logger.warning("Failed to delete sandbox %s: %s", sandbox_id, exc)

    remove_sandbox_id(project_key)
    return {"ok": True, "message": "Workspace deleted"}


@router.post("/cloud/projects/{project_name}/workspace/sync-to-sandbox")
async def sync_to_sandbox(
    project_name: str,
    http_request: Request,
) -> dict:
    """Sync project files from S3 to the Daytona sandbox.

    One-directional: S3 → Sandbox. Overwrites files in the sandbox
    with the current state from S3.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    sandbox_id = get_sandbox_id(project_key)
    if not sandbox_id:
        raise HTTPException(status_code=404, detail="No workspace provisioned for this project")

    client = await _require_daytona()

    # Ensure sandbox is running.
    info = await client.get_sandbox_by_id(sandbox_id)
    if info.state != "started":
        if info.state in ("stopped", "paused"):
            await client.start_sandbox(sandbox_id)
            await client.wait_for_sandbox(sandbox_id, target_state="started", timeout=60)
        else:
            raise HTTPException(
                status_code=409,
                detail=f"Sandbox is in state '{info.state}' — cannot sync until started",
            )

    project_dir = await client.get_project_dir(sandbox_id)

    await _sync_directory_from_s3_to_sandbox(
        client,
        sandbox_id,
        project_key,
        project_dir,
    )

    return {"ok": True, "message": "Files synced from S3 to sandbox"}


@router.post("/cloud/projects/{project_name}/workspace/sync-to-s3")
async def sync_to_s3(
    project_name: str,
    http_request: Request,
) -> dict:
    """Sync project files from the Daytona sandbox back to S3.

    One-directional: Sandbox → S3. Overwrites files in S3 with the
    current state from the sandbox (including .git, build artifacts, etc.).
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    sandbox_id = get_sandbox_id(project_key)
    if not sandbox_id:
        raise HTTPException(status_code=404, detail="No workspace provisioned for this project")

    client = await _require_daytona()

    # Ensure sandbox is running.
    info = await client.get_sandbox_by_id(sandbox_id)
    if info.state != "started":
        if info.state in ("stopped", "paused"):
            await client.start_sandbox(sandbox_id)
            await client.wait_for_sandbox(sandbox_id, target_state="started", timeout=60)
        else:
            raise HTTPException(
                status_code=409,
                detail=f"Sandbox is in state '{info.state}' — cannot sync until started",
            )

    project_dir = await client.get_project_dir(sandbox_id)

    await _sync_directory_from_sandbox_to_s3(
        client,
        sandbox_id,
        project_key,
        project_dir,
    )

    # Update the last synced timestamp.
    update_sync_timestamp(project_key)

    return {"ok": True, "message": "Files synced from sandbox to S3"}


@router.post("/cloud/projects/{project_name}/workspace/sync-and-finish")
async def sync_and_finish(
    project_name: str,
    http_request: Request,
) -> dict:
    """Sync project files from the Daytona sandbox back to S3.

    Use this when the agent has finished its edit-run-verify loop and the
    results should be persisted to the project's storage. This is the
    "commit and push" equivalent for Daytona-backed cloud projects.

    Sandbox is left running — does not stop it.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    sandbox_id = get_sandbox_id(project_key)
    if not sandbox_id:
        raise HTTPException(status_code=404, detail="No workspace provisioned for this project")

    client = await _require_daytona()

    # Ensure sandbox is running.
    info = await client.get_sandbox_by_id(sandbox_id)
    if info.state != "started":
        if info.state in ("stopped", "paused"):
            await client.start_sandbox(sandbox_id)
            await client.wait_for_sandbox(sandbox_id, target_state="started", timeout=60)
        else:
            raise HTTPException(
                status_code=409,
                detail=f"Sandbox is in state '{info.state}' — must be 'started' for sync",
            )

    project_dir = await client.get_project_dir(sandbox_id)

    await _sync_directory_from_sandbox_to_s3(
        client,
        sandbox_id,
        project_key,
        project_dir,
    )

    update_sync_timestamp(project_key)

    return {
        "ok": True,
        "message": "Files synced to S3",
        "sandbox_id": sandbox_id,
    }


@router.get("/cloud/projects/{project_name}/workspace/terminal")
async def get_workspace_terminal(
    project_name: str,
    http_request: Request,
) -> WorkspaceTerminalResponse:
    """Get the web terminal URL for a Daytona sandbox.

    Returns the preview URL for port 22222 — the built-in web terminal
    provided by Daytona. The frontend can open this URL in a new tab.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    sandbox_id = get_sandbox_id(project_key)
    if not sandbox_id:
        raise HTTPException(status_code=404, detail="No workspace provisioned for this project")

    client = await _require_daytona()

    # Ensure sandbox is running.
    info = await client.get_sandbox_by_id(sandbox_id)
    if info.state != "started":
        raise HTTPException(
            status_code=409,
            detail=f"Sandbox is in state '{info.state}' — must be 'started' for terminal access",
        )

    # Get the web terminal URL via the SDK (preview link on port 22222).
    web_terminal_url = await client.get_web_terminal_url(sandbox_id)

    return WorkspaceTerminalResponse(
        ok=True,
        web_terminal_url=web_terminal_url,
        sandbox_id=sandbox_id,
    )


class PortPreviewResponse(BaseModel):
    ok: bool
    url: str
    port: int
    sandbox_id: str


@router.get("/cloud/projects/{project_name}/workspace/preview")
async def get_port_preview(
    project_name: str,
    port: int = 8080,
    http_request: Request = None,
) -> PortPreviewResponse:
    """Get a public preview URL for a port in the sandbox.

    If you have a web server running inside the sandbox (e.g. a static
    file server, a dev server, an API), use this endpoint to get a URL
    you can open in your browser to view it.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    project_key = await _require_project(workspace_id, user_id, project_name)

    sandbox_id = get_sandbox_id(project_key)
    if not sandbox_id:
        raise HTTPException(status_code=404, detail="No workspace provisioned for this project")

    client = await _require_daytona()

    info = await client.get_sandbox_by_id(sandbox_id)
    if info.state != "started":
        raise HTTPException(
            status_code=409,
            detail=f"Sandbox is in state '{info.state}' — must be 'started' for port preview",
        )

    url = await client.get_port_preview_url(sandbox_id, port)

    return PortPreviewResponse(
        ok=True,
        url=url,
        port=port,
        sandbox_id=sandbox_id,
    )


# ── Sandbox file operations (Daytona sandbox as primary filesystem) ─────
#
# These endpoints let the frontend list, read, write, and manage files
# directly inside the Daytona sandbox — used when the explorer is in
# "daytona" mode for a cloud project with a provisioned sandbox.
#
# Paths in these endpoints are RELATIVE to the sandbox's project directory
# (e.g. "src/index.ts", "README.md"). The backend joins with the sandbox's
# project_dir to form the absolute path.
#
# Each endpoint:
#   1. Resolves workspace_id + user_id + project_name
#   2. Looks up the sandbox_id from the workspace map store
#   3. Ensures the sandbox is running
#   4. Translates the relative path to an absolute sandbox path
#   5. Delegates to the DaytonaClient


async def _require_sandbox(
    workspace_id: str,
    user_id: str,
    project_name: str,
) -> tuple[DaytonaClient, str, str, str]:
    """Resolve project → sandbox_id → sandbox project_dir.

    Returns ``(client, sandbox_id, project_key, project_dir)`` or raises
    HTTPException if the project or sandbox doesn't exist.
    """
    project_key = f"projects/{workspace_id}/{user_id}/{project_name}/"

    sandbox_id = get_sandbox_id(project_key)
    if not sandbox_id:
        raise HTTPException(
            status_code=404,
            detail="No workspace provisioned for this project — provision one first",
        )

    client = await _require_daytona()
    info = await client.get_sandbox_by_id(sandbox_id)
    if info.state != "started":
        raise HTTPException(
            status_code=409,
            detail=f"Sandbox is in state '{info.state}' — must be 'started' for file operations",
        )

    project_dir = await client.get_project_dir(sandbox_id)
    return client, sandbox_id, project_key, project_dir


def _sandbox_abs_path(project_dir: str, relative_path: str) -> str:
    """Join project_dir with a relative path, normalising slashes."""
    clean = relative_path.strip("/")
    if not clean or clean == ".":
        return project_dir
    return f"{project_dir}/{clean}"


@router.get("/cloud/projects/{project_name}/workspace/files/browse")
async def browse_sandbox_files(
    project_name: str,
    path: str = Query("", description="Relative path within the project"),
    http_request: Request = None,
) -> BrowseResponse:
    """List the contents of a directory inside the Daytona sandbox.

    ``path`` is relative to the project root. Empty string or ``"."``
    lists the project root.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    # Also verify the project marker exists in S3
    project_key = await _require_project(workspace_id, user_id, project_name)
    client, sandbox_id, _, project_dir = await _require_sandbox(
        workspace_id, user_id, project_name
    )

    remote_path = _sandbox_abs_path(project_dir, path)

    try:
        entries = await client.list_files(sandbox_id, remote_path)
    except Exception as exc:
        logger.warning("sandbox browse failed: path=%s  err=%s", remote_path, exc)
        return BrowseResponse(path=path, error="Could not list directory")

    files = [
        FileEntry(
            name=e.name,
            isDir=e.is_dir,
            size=str(e.size) if not e.is_dir and e.size > 0 else "",
        )
        for e in entries
    ]
    return BrowseResponse(path=path, files=files)


@router.get("/cloud/projects/{project_name}/workspace/files/content")
async def read_sandbox_file(
    project_name: str,
    path: str = Query(..., description="Relative file path within the project"),
    http_request: Request = None,
):
    """Read a file from the Daytona sandbox and return its raw content.

    ``path`` is relative to the project root. The response uses the
    file's MIME type for syntax highlighting in the browser.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    await _require_project(workspace_id, user_id, project_name)
    client, sandbox_id, _, project_dir = await _require_sandbox(
        workspace_id, user_id, project_name
    )

    if not path or path.strip("/") == "":
        raise HTTPException(status_code=400, detail="Cannot read a directory")

    remote_path = _sandbox_abs_path(project_dir, path)

    try:
        data = await client.download_file(sandbox_id, remote_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found in sandbox")
    except Exception as exc:
        logger.warning("sandbox read failed: path=%s  err=%s", remote_path, exc)
        raise HTTPException(status_code=500, detail="Could not read file") from exc

    # Guess MIME type from the file name
    import mimetypes

    mime, _ = mimetypes.guess_type(path)
    return Response(content=data, media_type=mime or "application/octet-stream")


@router.post("/cloud/projects/{project_name}/workspace/files/write")
async def write_sandbox_file(
    project_name: str,
    req: WriteFileRequest,
    http_request: Request = None,
) -> FileActionResponse:
    """Create or overwrite a file in the Daytona sandbox.

    The body ``path`` is relative to the project root. Parent directories
    are created automatically.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    await _require_project(workspace_id, user_id, project_name)
    client, sandbox_id, _, project_dir = await _require_sandbox(
        workspace_id, user_id, project_name
    )

    relative = req.path.strip("/")
    if not relative:
        raise HTTPException(status_code=400, detail="path must not be empty")

    remote_path = _sandbox_abs_path(project_dir, relative)

    # Create parent directory in sandbox.
    parent = os.path.dirname(remote_path)
    try:
        await client.create_folder(sandbox_id, parent)
    except Exception:
        pass

    try:
        await client.upload_bytes(
            sandbox_id, req.content.encode("utf-8"), remote_path
        )
    except Exception as exc:
        logger.warning("sandbox write failed: path=%s  err=%s", remote_path, exc)
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}") from exc

    return FileActionResponse(ok=True, path=relative)


@router.post("/cloud/projects/{project_name}/workspace/files/mkdir")
async def mkdir_sandbox(
    project_name: str,
    req: MkdirRequest,
    http_request: Request = None,
) -> FileActionResponse:
    """Create a directory inside the Daytona sandbox."""
    workspace_id, user_id = _resolve_ids(http_request)
    await _require_project(workspace_id, user_id, project_name)
    client, sandbox_id, _, project_dir = await _require_sandbox(
        workspace_id, user_id, project_name
    )

    relative = req.path.strip("/")
    if not relative:
        raise HTTPException(status_code=400, detail="path must not be empty")

    remote_path = _sandbox_abs_path(project_dir, relative)

    try:
        await client.create_folder(sandbox_id, remote_path, mode="755")
    except Exception as exc:
        logger.warning("sandbox mkdir failed: path=%s  err=%s", remote_path, exc)
        raise HTTPException(status_code=500, detail=f"Mkdir failed: {exc}") from exc

    return FileActionResponse(ok=True, path=relative)


@router.delete("/cloud/projects/{project_name}/workspace/files/delete")
async def delete_sandbox_item(
    project_name: str,
    path: str = Query(..., description="Relative path to delete"),
    recursive: bool = Query(False, description="Delete directories recursively"),
    http_request: Request = None,
) -> FileActionResponse:
    """Delete a file or directory inside the Daytona sandbox."""
    workspace_id, user_id = _resolve_ids(http_request)
    await _require_project(workspace_id, user_id, project_name)
    client, sandbox_id, _, project_dir = await _require_sandbox(
        workspace_id, user_id, project_name
    )

    relative = path.strip("/")
    if not relative:
        raise HTTPException(status_code=400, detail="path must not be empty")

    remote_path = _sandbox_abs_path(project_dir, relative)

    try:
        await client.delete_file(sandbox_id, remote_path, recursive=recursive)
    except Exception as exc:
        logger.warning("sandbox delete failed: path=%s  err=%s", remote_path, exc)
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}") from exc

    return FileActionResponse(ok=True)


@router.patch("/cloud/projects/{project_name}/workspace/files/rename")
async def rename_sandbox_item(
    project_name: str,
    req: RenameRequest,
    http_request: Request = None,
) -> FileActionResponse:
    """Rename or move a file/directory inside the Daytona sandbox.

    Both ``path`` and ``new_path`` are relative to the project root.
    Parent directories of the destination are created automatically.
    """
    workspace_id, user_id = _resolve_ids(http_request)
    await _require_project(workspace_id, user_id, project_name)
    client, sandbox_id, _, project_dir = await _require_sandbox(
        workspace_id, user_id, project_name
    )

    old_relative = req.path.strip("/")
    new_relative = req.new_path.strip("/")
    if not old_relative:
        raise HTTPException(status_code=400, detail="path must not be empty")
    if not new_relative:
        raise HTTPException(status_code=400, detail="new_path must not be empty")

    old_remote = _sandbox_abs_path(project_dir, old_relative)
    new_remote = _sandbox_abs_path(project_dir, new_relative)

    # Create parent directory of destination.
    parent = os.path.dirname(new_remote)
    try:
        await client.create_folder(sandbox_id, parent)
    except Exception:
        pass

    # Daytona SDK doesn't have a direct rename — we copy then delete.
    try:
        data = await client.download_file(sandbox_id, old_remote)
        await client.upload_bytes(sandbox_id, data, new_remote)
        await client.delete_file(sandbox_id, old_remote, recursive=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Source file not found")
    except Exception as exc:
        logger.warning(
            "sandbox rename failed: old=%s new=%s  err=%s",
            old_remote,
            new_remote,
            exc,
        )
        raise HTTPException(status_code=500, detail=f"Rename failed: {exc}") from exc

    return FileActionResponse(ok=True, path=new_relative)
