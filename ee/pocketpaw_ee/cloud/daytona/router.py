"""Daytona router — manage the workspace VM and project file operations.

Mounted at ``/api/v1/``. Endpoints:

**Workspace VM:**
  GET    /workspace/vm                                     — Get VM status (auto-provision)
  POST   /workspace/vm/provision                           — Force provision with config
  DELETE /workspace/vm                                     — Destroy VM
  GET    /workspace/vm/terminal                            — Get web terminal URL

**Project-in-VM file operations:**
  GET    /cloud/projects/{name}/vm/files/browse            — Browse project dir in VM
  GET    /cloud/projects/{name}/vm/files/content           — Read file from project in VM
  POST   /cloud/projects/{name}/vm/files/write             — Write file in project in VM
  POST   /cloud/projects/{name}/vm/files/mkdir             — Create dir in project in VM
  DELETE /cloud/projects/{name}/vm/files/delete            — Delete from project in VM
  PATCH  /cloud/projects/{name}/vm/files/rename            — Rename in project in VM

Created: 2026-06-24.  Updated: 2026-07-10 — workspace VM replaces per-project sandbox.
"""

from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from pocketpaw.api.v1.cloud_projects import _ADAPTER
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


class WorkspaceTerminalResponse(BaseModel):
    ok: bool
    web_terminal_url: str = ""
    sandbox_id: str = ""


# ══════════════════════════════════════════════════════════════════════════════
# WORKSPACE VM ENDPOINTS (NEW — primary path)
# ══════════════════════════════════════════════════════════════════════════════


class WorkspaceVmResponse(BaseModel):
    """Response for workspace VM status."""

    ok: bool
    has_vm: bool
    sandbox_id: str = ""
    sandbox_name: str = ""
    state: str = ""
    state_detail: str = ""
    workspace_root: str = ""
    config: dict = {}


class ProvisionWorkspaceVmRequest(BaseModel):
    """Request body for provisioning/re-provisioning the workspace VM."""

    cpu: int = 2
    memory: int = 4
    disk: int = 10
    root_dir: str = "/workspace"


def _resolve_workspace_id(http_request: Request) -> str:
    """Extract workspace_id from the request context.

    Reads ``X-Workspace-Id`` header, falling back to ``"default"``.
    """
    return http_request.headers.get("X-Workspace-Id", "default")


@router.get("/workspace/vm")
async def get_workspace_vm(
    http_request: Request,
) -> WorkspaceVmResponse:
    """Get workspace VM status — **auto-provisions** if no VM exists yet.

    This is the primary endpoint the frontend calls on mount. If the
    workspace doesn't have a VM, one is created automatically with the
    default config. The frontend polls until ``state == "started"``.
    """
    workspace_id = _resolve_workspace_id(http_request)

    # Guard: never auto-provision for "default" — this means the frontend
    # hasn't resolved the real workspace_id yet.  Returning no-VM here
    # prevents creating a stray "paw-ws-default" sandbox that will never
    # be used once the real workspace_id is known.
    if workspace_id == "default":
        return WorkspaceVmResponse(
            ok=True,
            has_vm=False,
            state="pending",
            state_detail="Waiting for workspace to be resolved…",
        )

    from pocketpaw_ee.cloud.daytona.store import (
        get_workspace_vm_config,
        get_workspace_vm_sandbox_id,
        set_workspace_vm,
    )

    sandbox_id = get_workspace_vm_sandbox_id(workspace_id)
    config = get_workspace_vm_config(workspace_id)

    if sandbox_id:
        # VM already exists — return status.
        try:
            client = await _require_daytona()
            info = await client.get_sandbox_by_id(sandbox_id)
            return WorkspaceVmResponse(
                ok=True,
                has_vm=True,
                sandbox_id=info.id,
                sandbox_name=info.name,
                state=info.state,
                state_detail=_sandbox_status_detail(info.state),
                workspace_root=config.get("root_dir", "/workspace"),
                config=config,
            )
        except Exception:
            # Sandbox might have been deleted externally — clean up and re-provision.
            from pocketpaw_ee.cloud.daytona.store import remove_workspace_vm as _rm_ws_vm

            _rm_ws_vm(workspace_id)
            sandbox_id = None

    # No VM exists or it was deleted — auto-provision.
    if not daytona_enabled():
        return WorkspaceVmResponse(
            ok=True,
            has_vm=False,
            state="config_error",
            state_detail="Daytona API is not configured",
            config=config,
        )

    try:
        client = await _require_daytona()
        sandbox_name = f"paw-ws-{workspace_id[:12]}"
        cpu = config.get("cpu", 2)
        memory = config.get("memory", 4)
        disk = config.get("disk", 10)
        auto_stop = config.get("auto_stop_interval", 3600)

        info = await client.create_sandbox(
            name=sandbox_name,
            cpu=cpu,
            memory=memory,
            disk=disk,
            auto_stop_interval=auto_stop,
        )
        set_workspace_vm(workspace_id, info.id, sandbox_name, config)

        # Create the workspace root directory inside the VM.
        root_dir = config.get("root_dir", "/workspace")
        asyncio.create_task(_ensure_workspace_root(client, info.id, root_dir))

        return WorkspaceVmResponse(
            ok=True,
            has_vm=True,
            sandbox_id=info.id,
            sandbox_name=sandbox_name,
            state=info.state,
            state_detail=_sandbox_status_detail(info.state),
            workspace_root=root_dir,
            config=config,
        )
    except Exception as exc:
        logger.exception("Failed to auto-provision workspace VM")
        return WorkspaceVmResponse(
            ok=True,
            has_vm=False,
            state="error",
            state_detail=f"Provisioning failed: {exc}",
            config=config,
        )


async def _ensure_workspace_root(client: DaytonaClient, sandbox_id: str, root_dir: str) -> None:
    """Wait for sandbox to start, then create the workspace root directory."""
    try:
        await client.wait_for_sandbox(sandbox_id, target_state="started", timeout=120)
        await client.create_folder(sandbox_id, root_dir)
        logger.info("Workspace root %s created in sandbox %s", root_dir, sandbox_id)
    except Exception as exc:
        logger.warning(
            "Failed to create workspace root %s in sandbox %s: %s",
            root_dir,
            sandbox_id,
            exc,
        )


@router.post("/workspace/vm/provision")
async def provision_workspace_vm(
    req: ProvisionWorkspaceVmRequest,
    http_request: Request,
) -> WorkspaceVmResponse:
    """Force (re)provision the workspace VM with the given config.

    If a VM already exists, it is destroyed first, then a new one is created.
    """
    workspace_id = _resolve_workspace_id(http_request)

    from pocketpaw_ee.cloud.daytona.store import (
        get_workspace_vm_sandbox_id,
        remove_workspace_vm,
        set_workspace_vm,
    )

    client = await _require_daytona()

    # Destroy existing VM if any.
    existing_id = get_workspace_vm_sandbox_id(workspace_id)
    if existing_id:
        try:
            await client.delete_sandbox(existing_id)
        except Exception as exc:
            logger.warning("Failed to delete existing workspace VM %s: %s", existing_id, exc)
        remove_workspace_vm(workspace_id)

    config = {
        "cpu": req.cpu,
        "memory": req.memory,
        "disk": req.disk,
        "root_dir": req.root_dir,
    }

    sandbox_name = f"paw-ws-{workspace_id[:12]}"
    info = await client.create_sandbox(
        name=sandbox_name,
        cpu=req.cpu,
        memory=req.memory,
        disk=req.disk,
    )
    set_workspace_vm(workspace_id, info.id, sandbox_name, config)

    asyncio.create_task(_ensure_workspace_root(client, info.id, req.root_dir))

    return WorkspaceVmResponse(
        ok=True,
        has_vm=True,
        sandbox_id=info.id,
        sandbox_name=sandbox_name,
        state=info.state,
        state_detail=_sandbox_status_detail(info.state),
        workspace_root=req.root_dir,
        config=config,
    )


@router.get("/workspace/vm/config")
async def get_workspace_vm_config(
    http_request: Request,
) -> dict:
    """Return the current workspace VM configuration (cpu, memory, disk, etc.)
    without touching the sandbox.  Returns defaults when no config exists."""
    workspace_id = _resolve_workspace_id(http_request)
    from pocketpaw_ee.cloud.daytona.store import get_workspace_vm_config

    config = get_workspace_vm_config(workspace_id)
    return {"ok": True, "config": config}


class UpdateWorkspaceVmConfigRequest(BaseModel):
    """Partial config update — every field optional."""

    cpu: int | None = None
    memory: int | None = None  # GB
    disk: int | None = None  # GB
    root_dir: str | None = None
    auto_stop_interval: int | None = None  # seconds


@router.patch("/workspace/vm/config")
async def update_workspace_vm_config(
    req: UpdateWorkspaceVmConfigRequest,
    http_request: Request,
) -> dict:
    """Update the workspace VM configuration WITHOUT re-provisioning.

    Changes take effect on the NEXT provision — the running VM is not
    resized.  To apply changes, stop and re-provision the VM.
    """
    workspace_id = _resolve_workspace_id(http_request)
    from pocketpaw_ee.cloud.daytona.store import (
        get_workspace_vm_config,
        update_workspace_vm_config,
    )

    updates = req.model_dump(exclude_none=True)
    if updates:
        update_workspace_vm_config(workspace_id, updates)

    config = get_workspace_vm_config(workspace_id)
    return {"ok": True, "config": config}


@router.post("/workspace/vm/stop")
async def stop_workspace_vm(
    http_request: Request,
) -> dict:
    """Stop the workspace VM without destroying it.

    The sandbox is paused — data on the VM disk is preserved.  Use
    GET /workspace/vm to resume (auto-provisions on next access if
    the auto-stop interval hasn't elapsed yet, or POST
    /workspace/vm/provision to force a fresh VM).
    """
    workspace_id = _resolve_workspace_id(http_request)

    from pocketpaw_ee.cloud.daytona.store import get_workspace_vm_sandbox_id

    sandbox_id = get_workspace_vm_sandbox_id(workspace_id)
    if not sandbox_id:
        return {"ok": True, "message": "No workspace VM to stop"}

    try:
        client = await _require_daytona()
        await client.stop_sandbox(sandbox_id)
    except Exception as exc:
        logger.warning("Failed to stop workspace VM %s: %s", sandbox_id, exc)
        raise HTTPException(status_code=500, detail=f"Stop failed: {exc}") from exc

    return {"ok": True, "message": "Workspace VM stopped", "state": "stopped"}


@router.delete("/workspace/vm")
async def delete_workspace_vm(
    http_request: Request,
) -> dict:
    """Destroy the workspace VM. All project data in the VM is lost.

    S3 storage is NOT affected — only the compute sandbox is destroyed.
    """
    workspace_id = _resolve_workspace_id(http_request)

    from pocketpaw_ee.cloud.daytona.store import (
        get_workspace_vm_sandbox_id,
        remove_workspace_vm,
    )

    sandbox_id = get_workspace_vm_sandbox_id(workspace_id)
    if not sandbox_id:
        return {"ok": True, "message": "No workspace VM to delete"}

    try:
        client = await _require_daytona()
        await client.delete_sandbox(sandbox_id)
    except Exception as exc:
        logger.warning("Failed to delete workspace VM %s: %s", sandbox_id, exc)

    remove_workspace_vm(workspace_id)
    return {"ok": True, "message": "Workspace VM deleted"}


@router.get("/workspace/vm/terminal")
async def get_workspace_vm_terminal(
    http_request: Request,
) -> WorkspaceTerminalResponse:
    """Get the web terminal URL for the workspace VM."""
    workspace_id = _resolve_workspace_id(http_request)

    from pocketpaw_ee.cloud.daytona.store import get_workspace_vm_sandbox_id

    sandbox_id = get_workspace_vm_sandbox_id(workspace_id)
    if not sandbox_id:
        raise HTTPException(status_code=404, detail="No workspace VM provisioned")

    client = await _require_daytona()
    info = await client.get_sandbox_by_id(sandbox_id)
    if info.state != "started":
        raise HTTPException(
            status_code=409,
            detail=f"VM is in state '{info.state}' — must be 'started' for terminal access",
        )

    web_terminal_url = await client.get_web_terminal_url(sandbox_id)
    return WorkspaceTerminalResponse(
        ok=True,
        web_terminal_url=web_terminal_url,
        sandbox_id=sandbox_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PROJECT-IN-VM FILE OPERATIONS (NEW)
# ══════════════════════════════════════════════════════════════════════════════
#
# These endpoints let the frontend list, read, write, and manage files
# inside a PROJECT SUBDIRECTORY within the workspace VM. When the user
# opens a cloud project, the file tree scopes to:
#
#     {workspace_root}/{project_name}/
#
# Each endpoint:
#   1. Resolves workspace_id from the request
#   2. Looks up the workspace VM sandbox
#   3. Ensures the VM is running
#   4. Joins the relative path with the project subdirectory path
#   5. Delegates to the DaytonaClient


async def _require_workspace_vm(
    workspace_id: str,
    project_name: str,
) -> tuple[DaytonaClient, str, str, str]:
    """Resolve workspace VM → sandbox → project path.

    Returns ``(client, sandbox_id, workspace_root, project_abs_path)``
    or raises HTTPException.
    """
    from pocketpaw_ee.cloud.daytona.store import (
        get_workspace_vm_config,
        get_workspace_vm_sandbox_id,
    )

    sandbox_id = get_workspace_vm_sandbox_id(workspace_id)
    if not sandbox_id:
        raise HTTPException(
            status_code=404,
            detail="No workspace VM provisioned — visit /workspace/vm first",
        )

    client = await _require_daytona()
    info = await client.get_sandbox_by_id(sandbox_id)
    if info.state != "started":
        raise HTTPException(
            status_code=409,
            detail=f"VM is in state '{info.state}' — must be 'started' for file operations",
        )

    config = get_workspace_vm_config(workspace_id)
    workspace_root = config.get("root_dir", "/workspace")
    project_abs_path = f"{workspace_root}/{project_name}".replace("//", "/")

    return client, sandbox_id, workspace_root, project_abs_path


def _vm_project_abs_path(project_abs: str, relative_path: str) -> str:
    """Join the project absolute path with a relative path."""
    clean = relative_path.strip("/")
    if not clean or clean == ".":
        return project_abs
    return f"{project_abs}/{clean}"


@router.get("/cloud/projects/{project_name}/vm/files/browse")
async def browse_vm_project_files(
    project_name: str,
    path: str = Query("", description="Relative path within the project"),
    http_request: Request = None,
) -> BrowseResponse:
    """List directory contents inside a project subdirectory in the workspace VM."""
    workspace_id = _resolve_workspace_id(http_request)
    client, sandbox_id, _, project_abs = await _require_workspace_vm(workspace_id, project_name)

    remote_path = _vm_project_abs_path(project_abs, path)

    try:
        entries = await client.list_files(sandbox_id, remote_path)
    except Exception as exc:
        logger.warning("vm project browse failed: path=%s  err=%s", remote_path, exc)
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


@router.get("/cloud/projects/{project_name}/vm/files/content")
async def read_vm_project_file(
    project_name: str,
    path: str = Query(..., description="Relative file path within the project"),
    http_request: Request = None,
):
    """Read a file from a project subdirectory in the workspace VM."""
    workspace_id = _resolve_workspace_id(http_request)
    client, sandbox_id, _, project_abs = await _require_workspace_vm(workspace_id, project_name)

    if not path or path.strip("/") == "":
        raise HTTPException(status_code=400, detail="Cannot read a directory")

    remote_path = _vm_project_abs_path(project_abs, path)

    try:
        data = await client.download_file(sandbox_id, remote_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found in VM")
    except Exception as exc:
        logger.warning("vm project read failed: path=%s  err=%s", remote_path, exc)
        raise HTTPException(status_code=500, detail="Could not read file") from exc

    import mimetypes

    mime, _ = mimetypes.guess_type(path)
    return Response(content=data, media_type=mime or "application/octet-stream")


@router.post("/cloud/projects/{project_name}/vm/files/write")
async def write_vm_project_file(
    project_name: str,
    req: WriteFileRequest,
    http_request: Request = None,
) -> FileActionResponse:
    """Create or overwrite a file in a project subdirectory in the workspace VM."""
    workspace_id = _resolve_workspace_id(http_request)
    client, sandbox_id, _, project_abs = await _require_workspace_vm(workspace_id, project_name)

    relative = req.path.strip("/")
    if not relative:
        raise HTTPException(status_code=400, detail="path must not be empty")

    remote_path = _vm_project_abs_path(project_abs, relative)

    parent = os.path.dirname(remote_path)
    try:
        await client.create_folder(sandbox_id, parent)
    except Exception:
        pass

    try:
        await client.upload_bytes(sandbox_id, req.content.encode("utf-8"), remote_path)
    except Exception as exc:
        logger.warning("vm project write failed: path=%s  err=%s", remote_path, exc)
        raise HTTPException(status_code=500, detail=f"Write failed: {exc}") from exc

    return FileActionResponse(ok=True, path=relative)


@router.post("/cloud/projects/{project_name}/vm/files/mkdir")
async def mkdir_vm_project(
    project_name: str,
    req: MkdirRequest,
    http_request: Request = None,
) -> FileActionResponse:
    """Create a directory inside a project subdirectory in the workspace VM."""
    workspace_id = _resolve_workspace_id(http_request)
    client, sandbox_id, _, project_abs = await _require_workspace_vm(workspace_id, project_name)

    relative = req.path.strip("/")
    if not relative:
        raise HTTPException(status_code=400, detail="path must not be empty")

    remote_path = _vm_project_abs_path(project_abs, relative)

    try:
        await client.create_folder(sandbox_id, remote_path, mode="755")
    except Exception as exc:
        logger.warning("vm project mkdir failed: path=%s  err=%s", remote_path, exc)
        raise HTTPException(status_code=500, detail=f"Mkdir failed: {exc}") from exc

    return FileActionResponse(ok=True, path=relative)


@router.delete("/cloud/projects/{project_name}/vm/files/delete")
async def delete_vm_project_item(
    project_name: str,
    path: str = Query(..., description="Relative path to delete"),
    recursive: bool = Query(False, description="Delete directories recursively"),
    http_request: Request = None,
) -> FileActionResponse:
    """Delete a file or directory from a project subdirectory in the workspace VM."""
    workspace_id = _resolve_workspace_id(http_request)
    client, sandbox_id, _, project_abs = await _require_workspace_vm(workspace_id, project_name)

    relative = path.strip("/")
    if not relative:
        raise HTTPException(status_code=400, detail="path must not be empty")

    remote_path = _vm_project_abs_path(project_abs, relative)

    try:
        await client.delete_file(sandbox_id, remote_path, recursive=recursive)
    except Exception as exc:
        logger.warning("vm project delete failed: path=%s  err=%s", remote_path, exc)
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}") from exc

    return FileActionResponse(ok=True)


@router.patch("/cloud/projects/{project_name}/vm/files/rename")
async def rename_vm_project_item(
    project_name: str,
    req: RenameRequest,
    http_request: Request = None,
) -> FileActionResponse:
    """Rename or move a file/directory in a project subdirectory in the workspace VM."""
    workspace_id = _resolve_workspace_id(http_request)
    client, sandbox_id, _, project_abs = await _require_workspace_vm(workspace_id, project_name)

    old_relative = req.path.strip("/")
    new_relative = req.new_path.strip("/")
    if not old_relative:
        raise HTTPException(status_code=400, detail="path must not be empty")
    if not new_relative:
        raise HTTPException(status_code=400, detail="new_path must not be empty")

    old_remote = _vm_project_abs_path(project_abs, old_relative)
    new_remote = _vm_project_abs_path(project_abs, new_relative)

    parent = os.path.dirname(new_remote)
    try:
        await client.create_folder(sandbox_id, parent)
    except Exception:
        pass

    try:
        data = await client.download_file(sandbox_id, old_remote)
        await client.upload_bytes(sandbox_id, data, new_remote)
        await client.delete_file(sandbox_id, old_remote, recursive=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Source file not found")
    except Exception as exc:
        logger.warning(
            "vm project rename failed: old=%s new=%s  err=%s",
            old_remote,
            new_remote,
            exc,
        )
        raise HTTPException(status_code=500, detail=f"Rename failed: {exc}") from exc

    return FileActionResponse(ok=True, path=new_relative)
