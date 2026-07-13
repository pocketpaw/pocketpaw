"""Daytona Python SDK wrapper.

Uses the official ``daytona`` SDK (``AsyncDaytona`` / ``AsyncSandbox``) for
sandbox lifecycle, file sync, process execution, and PTY terminal management.

Key types from the SDK:
  - ``AsyncDaytona`` — main client, configured via env or ``DaytonaConfig``
  - ``AsyncSandbox`` — a VM/container workspace, created via ``AsyncDaytona.create()``
  - ``AsyncSandbox.fs``  — :class:`AsyncFileSystem`  (upload, download, list, …)
  - ``AsyncSandbox.process`` — :class:`AsyncProcess` (exec, PTY sessions, …)
  - ``AsyncSandbox.git``    — :class:`AsyncGit`      (clone, status, branches, …)

Moved from OSS to EE: 2026-06-24
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from daytona import (
    AsyncDaytona,
    AsyncSandbox,
    CreateSandboxFromImageParams,
    DaytonaConfig,
    ExecuteResponse,
    FileUpload,
    Image,
    PtySize,
    Resources,
)
from daytona._async.filesystem import AsyncFileSystem, FileInfo
from daytona._async.git import GitStatus

from pocketpaw_ee.cloud.daytona.config import daytona_api_key, daytona_api_url
from pocketpaw_ee.cloud.daytona.image import resolve_sandbox_image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SandboxInfo:
    """Represents a Daytona sandbox (workspace/VM)."""

    id: str
    name: str
    state: str  # creating, started, stopped, error, etc.
    organization_id: str = ""
    cpu: int = 1
    memory: int = 2  # GB
    disk: int = 10  # GB
    region_id: str = ""
    created_at: str = ""
    last_activity_at: str = ""


@dataclass
class DaytonaWorkspaceMapping:
    """Maps a cloud project to a Daytona sandbox."""

    project_name: str
    workspace_id: str
    user_id: str
    sandbox_id: str
    sandbox_name: str
    created_at: str = ""
    last_synced_at: str = ""


# ---------------------------------------------------------------------------
# Client wrapper
# ---------------------------------------------------------------------------


class DaytonaClient:
    """Thin wrapper over the official ``daytona`` Python SDK.

    Provides convenience methods for sandbox lifecycle and delegates file,
    process, and git operations to the underlying ``AsyncSandbox``.
    """

    def __init__(self) -> None:
        self._daytona: AsyncDaytona | None = None
        self._api_key = daytona_api_key()
        # The SDK expects the full API URL (with /api) as the host — it
        # appends endpoint paths like /sandbox directly via its Swagger
        # client.  Do NOT strip /api here.
        self._api_url = daytona_api_url().rstrip("/")
        if not self._api_url.endswith("/api"):
            self._api_url = f"{self._api_url}/api"

    async def _ensure_daytona(self) -> AsyncDaytona:
        if self._daytona is None:
            config = DaytonaConfig(
                api_key=self._api_key,
                api_url=self._api_url,
            )
            self._daytona = AsyncDaytona(config)
        return self._daytona

    async def close(self) -> None:
        if self._daytona is not None:
            await self._daytona.close()
            self._daytona = None

    # ── Web terminal (preview URL on port 22222) ──────────────────────

    async def get_web_terminal_url(self, sandbox_id: str) -> str:
        """Get the web terminal URL for a sandbox.

        Daytona sandboxes have a built-in web terminal on port 22222.
        This uses the SDK's ``get_preview_link()`` to obtain the URL
        and appends the access token as a query parameter.
        """
        sb = await self.get_sandbox_instance(sandbox_id)
        preview = await sb.get_preview_link(22222)
        url = preview.url if hasattr(preview, "url") else str(preview)
        token = preview.token if hasattr(preview, "token") else ""
        # Append token if not already in the URL.
        if token and "token=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={token}"
        logger.debug("Got web terminal URL for sandbox %s", sandbox_id)
        return url

    # ── Port forwarding preview (any port) ───────────────────────────

    async def get_port_preview_url(self, sandbox_id: str, port: int) -> str:
        """Get a public preview URL for a port running in the sandbox.

        Uses the SDK's ``get_preview_link()`` to obtain the URL and
        appends the access token so the browser can authenticate.
        This is how you access a web server running inside the sandbox
        (e.g. ``python3 -m http.server 8080``) from your own machine.
        """
        sb = await self.get_sandbox_instance(sandbox_id)
        preview = await sb.get_preview_link(port)
        url = preview.url if hasattr(preview, "url") else str(preview)
        token = preview.token if hasattr(preview, "token") else ""
        if token and "token=" not in url:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={token}"
        logger.debug("Got port preview URL for sandbox %s port %d: %s", sandbox_id, port, url)
        return url

    # ── Sandbox lifecycle ────────────────────────────────────────────────

    async def create_sandbox(
        self,
        name: str,
        image: str | None = None,
        language: str | None = None,
        cpu: int = 2,
        memory: int = 4,
        disk: int = 10,
        auto_stop_interval: int = 3600,
    ) -> SandboxInfo:
        """Create a new sandbox using the SDK.

        Returns immediately; call ``wait_for_sandbox()`` to block until
        the sandbox reaches the ``started`` state.

        The *image* parameter can be:

          * ``None`` — uses the pre-built Paw development image (Python,
            Node.js, GCC, Docker, UV, and common CLI tools).  See
            :func:`resolve_sandbox_image` for override logic.
          * A ``str`` — a regular Docker image reference (e.g.
            ``"ubuntu:latest"``, ``"python:3.12"``).
          * An ``Image`` object — dynamically built via the Daytona SDK.

        Args:
            name: Sandbox name.
            image: Image to use.  ``None`` → pre-built Paw dev image.
            language: Optional code language hint (``python``, ``typescript``, etc.).
            cpu: Number of CPUs.
            memory: Memory in GB.
            disk: Disk size in GB.
            auto_stop_interval: Seconds of inactivity before auto-stop.
        """
        daytona = await self._ensure_daytona()

        # Resolve the image — use the pre-built Paw dev image when none
        # is explicitly given.
        resolved_image: str | Image
        if image is None:
            resolved_image = resolve_sandbox_image()
        else:
            resolved_image = image

        logger.info(
            "Creating Daytona sandbox: name=%s image=%s cpu=%d mem=%d disk=%d",
            name,
            resolved_image if isinstance(resolved_image, str) else "<dynamic-image>",
            cpu,
            memory,
            disk,
        )

        params = CreateSandboxFromImageParams(
            name=name,
            image=resolved_image,
            resources=Resources(cpu=cpu, memory=memory, disk=disk),
            auto_stop_interval=auto_stop_interval,
        )
        if language:
            params.language = language

        sandbox = await daytona.create(params)
        sb = await self._parse_sandbox(sandbox)
        logger.info("Sandbox created: id=%s name=%s state=%s", sb.id, sb.name, sb.state)
        return sb

    async def get_sandbox_by_id(self, sandbox_id: str) -> SandboxInfo:
        """Get sandbox details by ID using the SDK."""
        daytona = await self._ensure_daytona()
        sandbox = await daytona.get(sandbox_id)
        return await self._parse_sandbox(sandbox)

    async def list_sandboxes(self) -> list[SandboxInfo]:
        """List all sandboxes via the SDK."""
        daytona = await self._ensure_daytona()
        results = []
        async for sb in daytona.list():
            results.append(await self._parse_sandbox(sb))
        return results

    async def find_sandbox_by_name(self, name: str) -> SandboxInfo | None:
        """Find a sandbox by name using the SDK."""
        daytona = await self._ensure_daytona()
        async for sb in daytona.list():
            if sb.name == name:
                return await self._parse_sandbox(sb)
        return None

    async def delete_sandbox(self, sandbox_id: str) -> None:
        """Delete a sandbox (destroys the VM)."""
        sb = await self.get_sandbox_instance(sandbox_id)
        logger.info("Deleting sandbox: id=%s", sandbox_id)
        await sb.delete()
        logger.info("Sandbox deleted: id=%s", sandbox_id)

    async def start_sandbox(self, sandbox_id: str) -> None:
        """Start or resume a sandbox."""
        sb = await self.get_sandbox_instance(sandbox_id)
        await sb.start()

    async def stop_sandbox(self, sandbox_id: str) -> None:
        """Stop a sandbox."""
        sb = await self.get_sandbox_instance(sandbox_id)
        await sb.stop()

    async def wait_for_sandbox(
        self,
        sandbox_id: str,
        target_state: str = "started",
        timeout: float = 120.0,
        poll_interval: float = 2.0,
    ) -> SandboxInfo:
        """Poll until the sandbox reaches *target_state*.

        Uses the SDK's ``wait_for_sandbox_start()`` when target is ``started``;
        otherwise falls back to manual polling.
        """
        if target_state == "started":
            daytona = await self._ensure_daytona()
            sandbox = await daytona.get(sandbox_id)
            await sandbox.wait_for_sandbox_start(timeout=timeout)
            return await self._parse_sandbox(sandbox)
        else:
            # Fallback: manual polling for non-started targets
            deadline = asyncio.get_event_loop().time() + timeout
            daytona = await self._ensure_daytona()
            while True:
                sandbox = await daytona.get(sandbox_id)
                info = await self._parse_sandbox(sandbox)
                if info.state == target_state:
                    return info
                if info.state in ("error", "build_failed"):
                    raise RuntimeError(f"Sandbox {sandbox_id} entered error state: {info.state}")
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError(
                        f"Sandbox {sandbox_id} did not reach state '{target_state}' "
                        f"within {timeout}s (current state: {info.state})"
                    )
                await asyncio.sleep(poll_interval)

    # ── Get an AsyncSandbox instance for file/process/git operations ────

    async def get_sandbox_instance(self, sandbox_id: str) -> AsyncSandbox:
        """Return the SDK ``AsyncSandbox`` for direct file/process/git access.

        Use this when you need to call ``.fs.*``, ``.process.*``, or ``.git.*``.
        """
        daytona = await self._ensure_daytona()
        sandbox = await daytona.get(sandbox_id)
        return sandbox

    # ── File operations (delegate to AsyncSandbox.fs) ────────────────────

    async def get_fs(self, sandbox_id: str) -> AsyncFileSystem:
        """Shortcut to get the AsyncFileSystem for a sandbox."""
        sb = await self.get_sandbox_instance(sandbox_id)
        return sb.fs

    async def upload_file(self, sandbox_id: str, local_path: str, remote_path: str) -> None:
        """Upload a file to the sandbox using the SDK."""
        fs = await self.get_fs(sandbox_id)
        await fs.upload_file(local_path, remote_path)

    async def upload_bytes(self, sandbox_id: str, data: bytes, remote_path: str) -> None:
        """Upload raw bytes to a path in the sandbox."""
        fs = await self.get_fs(sandbox_id)
        await fs.upload_file(data, remote_path)

    async def bulk_upload(self, sandbox_id: str, files: list[tuple[str | bytes, str]]) -> None:
        """Upload multiple files to the sandbox.

        Each tuple is ``(source, remote_path)`` where *source* is either a
        local file path (``str``) or raw ``bytes``.
        """
        fs = await self.get_fs(sandbox_id)
        uploads: list[FileUpload] = []
        for src, dst in files:
            uploads.append(FileUpload(source=src, destination=dst))
        await fs.upload_files(uploads)

    async def download_file(self, sandbox_id: str, remote_path: str) -> bytes:
        """Download a file from the sandbox. Returns the bytes."""
        fs = await self.get_fs(sandbox_id)
        result = await fs.download_file(remote_path)
        if result is None:
            raise FileNotFoundError(f"File not found in sandbox: {remote_path}")
        return result

    async def download_to_path(self, sandbox_id: str, remote_path: str, local_path: str) -> None:
        """Download a file from the sandbox to a local path."""
        data = await self.download_file(sandbox_id, remote_path)
        with open(local_path, "wb") as f:
            f.write(data)

    async def list_files(self, sandbox_id: str, path: str = ".") -> list[FileInfo]:
        """List files in a directory inside the sandbox."""
        fs = await self.get_fs(sandbox_id)
        return await fs.list_files(path)

    async def create_folder(self, sandbox_id: str, path: str, mode: str = "755") -> None:
        """Create a directory in the sandbox.

        The *mode* defaults to ``"755"`` (rwxr-xr-x).
        """
        fs = await self.get_fs(sandbox_id)
        await fs.create_folder(path, mode)

    async def delete_file(self, sandbox_id: str, path: str, recursive: bool = False) -> None:
        """Delete a file or directory in the sandbox."""
        fs = await self.get_fs(sandbox_id)
        await fs.delete_file(path, recursive=recursive)

    async def get_work_dir(self, sandbox_id: str) -> str:
        """Get the sandbox's work directory path."""
        sb = await self.get_sandbox_instance(sandbox_id)
        return await sb.get_work_dir()

    async def get_project_dir(self, sandbox_id: str) -> str:
        """Get the sandbox's project directory path."""
        sb = await self.get_sandbox_instance(sandbox_id)
        return await sb.get_user_root_dir()

    # ── Git operations (delegate to AsyncSandbox.git) ────────────────────

    async def git_clone(
        self,
        sandbox_id: str,
        repo_url: str,
        path: str,
        branch: str | None = None,
        commit_id: str | None = None,
    ) -> None:
        """Clone a git repository into the sandbox."""
        sb = await self.get_sandbox_instance(sandbox_id)
        await sb.git.clone(repo_url, path, branch=branch, commit_id=commit_id)

    async def git_status(self, sandbox_id: str, path: str) -> GitStatus:
        """Get git status in the sandbox."""
        sb = await self.get_sandbox_instance(sandbox_id)
        return await sb.git.status(path)

    # ── Process execution (delegate to AsyncSandbox.process) ─────────────

    async def execute_command(
        self,
        sandbox_id: str,
        command: str,
        cwd: str | None = None,
        timeout: int = 30,
    ) -> ExecuteResponse:
        """Execute a command in the sandbox."""
        sb = await self.get_sandbox_instance(sandbox_id)
        return await sb.process.exec(command, cwd=cwd, timeout=timeout)

    # ── PTY terminal ──────────────────────────────────────────────────

    async def create_pty_session(
        self,
        sandbox_id: str,
        session_id: str,
        on_data: Callable[[bytes], Any] | None = None,
        cwd: str | None = None,
        pty_size: PtySize | None = None,
    ) -> None:
        """Create a PTY session in the sandbox.

        The SDK manages the WebSocket internally. *on_data* is called with
        raw terminal output bytes. Returns immediately; the session lives
        until ``kill_pty_session`` is called.
        """
        sb = await self.get_sandbox_instance(sandbox_id)

        async def _default_handler(data: bytes) -> None:
            logger.debug("PTY output (first 200): %s", data[:200])

        handler = on_data if on_data else _default_handler
        await sb.process.create_pty_session(session_id, handler, cwd=cwd, pty_size=pty_size)

    async def resize_pty(self, sandbox_id: str, session_id: str, cols: int, rows: int) -> None:
        """Resize a PTY session."""
        sb = await self.get_sandbox_instance(sandbox_id)
        await sb.process.resize_pty_session(session_id, PtySize(cols=cols, rows=rows))

    async def kill_pty(self, sandbox_id: str, session_id: str) -> None:
        """Kill a PTY session."""
        sb = await self.get_sandbox_instance(sandbox_id)
        await sb.process.kill_pty_session(session_id)

    # ── Helpers ──────────────────────────────────────────────────────────

    async def _parse_sandbox(self, sandbox: AsyncSandbox) -> SandboxInfo:
        """Extract SandboxInfo from an SDK AsyncSandbox instance."""
        return SandboxInfo(
            id=sandbox.id,
            name=sandbox.name,
            state=sandbox.state.value if hasattr(sandbox.state, "value") else str(sandbox.state),
            organization_id=sandbox.organization_id,
            cpu=getattr(sandbox, "cpu", 1) or 1,
            memory=getattr(sandbox, "memory", 2) or 2,
            disk=getattr(sandbox, "disk", 10) or 10,
            region_id=getattr(sandbox, "region_id", ""),
            created_at=getattr(sandbox, "created_at", ""),
            last_activity_at=getattr(sandbox, "last_activity_at", ""),
        )


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_client: DaytonaClient | None = None


def get_daytona_client() -> DaytonaClient | None:
    """Return the singleton Daytona client, or None if Daytona is not configured."""
    global _client
    from pocketpaw_ee.cloud.daytona.config import daytona_enabled

    if not daytona_enabled():
        return None
    if _client is None:
        _client = DaytonaClient()
    return _client


async def close_daytona_client() -> None:
    """Close the Daytona client session (for shutdown)."""
    global _client
    if _client:
        await _client.close()
        _client = None
