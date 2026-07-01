"""Daytona-aware tool wrappers — route file/shell/exec operations through the
sandbox VM when a Daytona context is active.

Each tool mirrors the name, description, and parameter schema of its OSS
counterpart (``ReadFileTool``, ``WriteFileTool``, etc.) so it naturally
replaces it in the ``ToolRegistry`` via last-writer-wins registration.

At execution time the tool calls ``resolve_daytona_context()``. If a sandbox
is active, the operation is routed through the Daytona SDK (download, upload,
exec). If no sandbox context exists, the tool falls back to the SAME local-FS
implementation as the OSS original — so these wrappers are safe to register
on any install; they only change behaviour when a sandbox is provisioned.

Created: 2026-07-01
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from pocketpaw.config import get_settings
from pocketpaw.security import get_guardian
from pocketpaw.security.rails import COMPILED_DANGEROUS_PATTERNS
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_project_prefix(path: str, ctx: Any) -> str:
    """Strip the local storage prefix from *path* to get the relative path
    inside the sandbox.

    Handles both S3 key prefixes and local-disk paths:
      ``projects/{ws}/{uid}/{name}/src/main.py`` → ``src/main.py``
      ``~/.pocketpaw/uploads/projects/{ws}/{uid}/{name}/src/main.py`` → ``src/main.py``

    When no known prefix matches, returns the path as-is (relative to the
    sandbox project dir).
    """
    # Clean the path — expanduser, resolve symlinks, normalize.
    clean = path.strip()

    # Try to extract the relative path by matching the project_key prefix.
    # The S3 key prefix is like "projects/{ws}/{uid}/{name}/"
    project_key = ctx.project_key  # e.g. "projects/ws123/user456/my-app/"
    if project_key in clean:
        idx = clean.index(project_key) + len(project_key)
        rel = clean[idx:].lstrip("/")
        if rel:
            return rel
        return "."  # the project root itself

    # Try the full local path prefix.
    # The local path is like "~/.pocketpaw/uploads/projects/{ws}/{uid}/{name}/"
    local_prefix = (Path.home() / ".pocketpaw" / "uploads" / project_key).as_posix()
    if local_prefix in clean:
        idx = clean.index(local_prefix) + len(local_prefix)
        rel = clean[idx:].lstrip("/")
        if rel:
            return rel
        return "."

    # If clean starts with ~/ or /home/ or the project name, try heuristic.
    path_obj = Path(clean)
    if ctx.project_name and ctx.project_name in clean:
        idx = clean.index(ctx.project_name) + len(ctx.project_name)
        rel = clean[idx:].lstrip("/")
        if rel:
            return rel

    # Fallback: return the basename (assumed to be a file in project root).
    return path_obj.name


# ---------------------------------------------------------------------------
# ReadFileTool — Daytona-aware
# ---------------------------------------------------------------------------


class DaytonaReadFileTool(BaseTool):
    """Read file contents — routes through Daytona sandbox when active."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read",
                },
                "encoding": {
                    "type": "string",
                    "description": "File encoding (default: utf-8)",
                    "default": "utf-8",
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, encoding: str = "utf-8") -> str:
        from pocketpaw_ee.cloud.daytona.context import resolve_daytona_context

        ctx = await resolve_daytona_context()
        if ctx is not None:
            try:
                rel_path = _strip_project_prefix(path, ctx)
                remote_path = f"{ctx.project_dir}/{rel_path}".replace("//", "/")
                data = await ctx.client.download_file(ctx.sandbox_id, remote_path)
                content = data.decode(encoding)
                if len(content) > 100_000:
                    content = content[:100_000] + "\n\n...(truncated, file too large)"
                return content
            except Exception as exc:
                return self._error(f"Failed to read from sandbox: {exc}")

        # Fallback: local FS (same as OSS ReadFileTool)
        try:
            file_path = Path(path).expanduser().resolve()
            jail = get_settings().file_jail_path.resolve()
            from pocketpaw.tools.fetch import is_safe_path

            if not is_safe_path(file_path, jail):
                return self._error(f"Access denied: {path} is outside allowed directory")
            if not file_path.exists():
                return self._error(f"File not found: {path}")
            if not file_path.is_file():
                return self._error(f"Not a file: {path}")
            content = file_path.read_text(encoding=encoding)
            if len(content) > 100_000:
                content = content[:100_000] + "\n\n...(truncated, file too large)"
            return content
        except UnicodeDecodeError:
            return self._error(f"Cannot read {path}: not a text file or wrong encoding")
        except Exception as e:
            return self._error(str(e))


# ---------------------------------------------------------------------------
# WriteFileTool — Daytona-aware
# ---------------------------------------------------------------------------


class DaytonaWriteFileTool(BaseTool):
    """Write content to a file — routes through Daytona sandbox when active."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file. Creates the file if it doesn't exist."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str) -> str:
        from pocketpaw_ee.cloud.daytona.context import resolve_daytona_context

        ctx = await resolve_daytona_context()
        if ctx is not None:
            try:
                rel_path = _strip_project_prefix(path, ctx)
                remote_path = f"{ctx.project_dir}/{rel_path}".replace("//", "/")

                # Ensure parent directory exists in sandbox.
                parent = os.path.dirname(remote_path)
                try:
                    await ctx.client.create_folder(ctx.sandbox_id, parent)
                except Exception:
                    pass  # dir may already exist

                await ctx.client.upload_bytes(ctx.sandbox_id, content.encode("utf-8"), remote_path)
                chars = len(content)
                return f"Successfully wrote {chars} characters to {path} (via Daytona sandbox)"
            except Exception as exc:
                return self._error(f"Failed to write to sandbox: {exc}")

        # Fallback: local FS
        try:
            file_path = Path(path).expanduser().resolve()
            jail = get_settings().file_jail_path.resolve()
            from pocketpaw.tools.fetch import is_safe_path

            if not is_safe_path(file_path, jail):
                return self._error(f"Access denied: {path} is outside allowed directory")
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            return f"Successfully wrote {len(content)} characters to {path}"
        except Exception as e:
            return self._error(str(e))


# ---------------------------------------------------------------------------
# EditFileTool — Daytona-aware
# ---------------------------------------------------------------------------


class DaytonaEditFileTool(BaseTool):
    """Edit a file by replacing an exact string match — routes through
    Daytona sandbox when active."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing an exact string match with new content. "
            "The old_string must appear exactly once in the file for the edit to succeed, "
            "unless replace_all is set to true."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find and replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences instead of requiring uniqueness",
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
        }

    async def execute(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        from pocketpaw_ee.cloud.daytona.context import resolve_daytona_context

        ctx = await resolve_daytona_context()
        if ctx is not None:
            try:
                rel_path = _strip_project_prefix(path, ctx)
                remote_path = f"{ctx.project_dir}/{rel_path}".replace("//", "/")

                # Download current content.
                data = await ctx.client.download_file(ctx.sandbox_id, remote_path)
                content = data.decode("utf-8")

                count = content.count(old_string)
                if count == 0:
                    return self._error("old_string not found in file")
                if not replace_all and count > 1:
                    return self._error(
                        f"old_string appears {count} times. Provide more context to make it "
                        f"unique, or set replace_all=true"
                    )

                new_content = content.replace(old_string, new_string)
                replacements = count if replace_all else 1

                # Upload modified content.
                await ctx.client.upload_bytes(
                    ctx.sandbox_id, new_content.encode("utf-8"), remote_path
                )
                return (
                    f"Successfully made {replacements} replacement(s) in {path}"
                    f" (via Daytona sandbox)"
                )
            except Exception as exc:
                return self._error(f"Failed to edit in sandbox: {exc}")

        # Fallback: local FS
        try:
            file_path = Path(path).expanduser().resolve()
            jail = get_settings().file_jail_path.resolve()
            from pocketpaw.tools.fetch import is_safe_path

            if not is_safe_path(file_path, jail):
                return self._error(f"Access denied: {path} is outside allowed directory")
            if not file_path.exists():
                return self._error(f"File not found: {path}")
            if not file_path.is_file():
                return self._error(f"Not a file: {path}")

            content = file_path.read_text(encoding="utf-8")
            count = content.count(old_string)
            if count == 0:
                return self._error("old_string not found in file")
            if not replace_all and count > 1:
                return self._error(
                    f"old_string appears {count} times. Provide more context to make it "
                    f"unique, or set replace_all=true"
                )
            new_content = content.replace(old_string, new_string)
            file_path.write_text(new_content, encoding="utf-8")
            replacements = count if replace_all else 1
            return f"Successfully made {replacements} replacement(s) in {path}"
        except UnicodeDecodeError:
            return self._error(f"Cannot read {path}: not a text file or wrong encoding")
        except Exception as e:
            return self._error(str(e))


# ---------------------------------------------------------------------------
# ListDirTool — Daytona-aware
# ---------------------------------------------------------------------------


class DaytonaListDirTool(BaseTool):
    """List directory contents — routes through Daytona sandbox when active."""

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List the contents of a directory."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the directory to list",
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "Show hidden files (default: false)",
                    "default": False,
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, show_hidden: bool = False) -> str:
        from pocketpaw_ee.cloud.daytona.context import resolve_daytona_context

        ctx = await resolve_daytona_context()
        if ctx is not None:
            try:
                rel_path = _strip_project_prefix(path, ctx)
                remote_path = f"{ctx.project_dir}/{rel_path}".replace("//", "/")

                entries = await ctx.client.list_files(ctx.sandbox_id, remote_path)
                items = []
                for entry in entries:
                    if not show_hidden and entry.name.startswith("."):
                        continue
                    prefix = "[DIR] " if entry.is_dir else "[FILE]"
                    size = ""
                    if not entry.is_dir and entry.size > 0:
                        size = f" ({entry.size} bytes)"
                    items.append(f"{prefix} {entry.name}{size}")

                if not items:
                    return "(empty directory)"
                return "\n".join(items)
            except Exception as exc:
                return self._error(f"Failed to list directory in sandbox: {exc}")

        # Fallback: local FS
        try:
            dir_path = Path(path).expanduser().resolve()
            jail = get_settings().file_jail_path.resolve()
            from pocketpaw.tools.fetch import is_safe_path

            if not is_safe_path(dir_path, jail):
                return self._error(f"Access denied: {path} is outside allowed directory")
            if not dir_path.exists():
                return self._error(f"Directory not found: {path}")
            if not dir_path.is_dir():
                return self._error(f"Not a directory: {path}")

            items = []
            for item in sorted(dir_path.iterdir()):
                if not show_hidden and item.name.startswith("."):
                    continue
                prefix = "[DIR] " if item.is_dir() else "[FILE]"
                size = ""
                if item.is_file():
                    size = f" ({item.stat().st_size} bytes)"
                items.append(f"{prefix} {item.name}{size}")

            if not items:
                return "(empty directory)"
            return "\n".join(items)
        except Exception as e:
            return self._error(str(e))


# ---------------------------------------------------------------------------
# ShellTool — Daytona-aware
# ---------------------------------------------------------------------------


class DaytonaShellTool(BaseTool):
    """Execute shell commands — runs in Daytona sandbox when active."""

    DANGEROUS_PATTERNS = COMPILED_DANGEROUS_PATTERNS

    def __init__(self, working_dir: str | None = None, timeout: int = 120):
        self._working_dir = working_dir
        self.timeout = timeout

    @property
    def name(self) -> str:
        return "shell"

    @property
    def description(self) -> str:
        return "Execute a shell command and return the output."

    @property
    def trust_level(self) -> str:
        return "critical"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                }
            },
            "required": ["command"],
        }

    async def execute(self, command: str) -> str:
        from pocketpaw_ee.cloud.daytona.context import resolve_daytona_context

        ctx = await resolve_daytona_context()
        if ctx is not None:
            return await self._execute_in_sandbox(command, ctx)

        # Fallback: local FS (same as OSS ShellTool)
        return await self._execute_locally(command)

    async def _execute_in_sandbox(self, command: str, ctx: Any) -> str:
        """Execute a command inside the Daytona sandbox VM."""
        # Security checks still apply even in the sandbox.
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern.search(command):
                return self._error(f"Dangerous command blocked: {command}")

        is_safe, reason = await get_guardian().check_command(command)
        if not is_safe:
            return self._error(f"Command blocked by Guardian: {reason}")

        try:
            result = await ctx.client.execute_command(
                ctx.sandbox_id,
                command,
                cwd=ctx.project_dir,
                timeout=self.timeout,
            )
            output = result.result or ""
            if result.exit_code != 0:
                output += f"\n\nExit code: {result.exit_code}"
            return output.strip() or "(no output)"
        except Exception as exc:
            return self._error(f"Sandbox command failed: {exc}")

    async def _execute_locally(self, command: str) -> str:
        """Execute a command locally (OSS ShellTool fallback)."""
        if command.strip() == "pwd":
            cwd = self._working_dir or str(get_settings().file_jail_path)
            return cwd

        for pattern in self.DANGEROUS_PATTERNS:
            if pattern.search(command):
                return self._error(f"Dangerous command blocked: {command}")

        is_safe, reason = await get_guardian().check_command(command)
        if not is_safe:
            return self._error(f"Command blocked by Guardian: {reason}")

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=self._working_dir or str(get_settings().file_jail_path),
                ),
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n\nExit code: {result.returncode}"
            return output.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return self._error(f"Command timed out after {self.timeout}s")
        except Exception as e:
            return self._error(str(e))


# ---------------------------------------------------------------------------
# RunPythonTool — Daytona-aware
# ---------------------------------------------------------------------------


class DaytonaRunPythonTool(BaseTool):
    """Execute Python code — runs in Daytona sandbox when active."""

    @property
    def name(self) -> str:
        return "run_python"

    @property
    def description(self) -> str:
        return (
            "Execute a Python script in a sandboxed subprocess and return its output. "
            "Use for data processing, file generation, calculations, or running installed packages."
        )

    @property
    def trust_level(self) -> str:
        return "critical"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default: 120)",
                    "default": 120,
                },
            },
            "required": ["code"],
        }

    async def execute(self, code: str, timeout: int = 120) -> str:
        from pocketpaw_ee.cloud.daytona.context import resolve_daytona_context

        ctx = await resolve_daytona_context()
        if ctx is not None:
            return await self._execute_in_sandbox(code, timeout, ctx)

        # Fallback: local FS (same as OSS RunPythonTool)
        return await self._execute_locally(code, timeout)

    async def _execute_in_sandbox(self, code: str, timeout: int, ctx: Any) -> str:
        """Run Python code inside the Daytona sandbox."""
        is_safe, reason = await get_guardian().check_command(code)
        if not is_safe:
            return self._error(f"Code blocked by Guardian: {reason}")

        # Write code to a temp file in the sandbox and execute it.
        script_name = f"_paw_run_{uuid.uuid4().hex}.py"
        remote_script = f"{ctx.project_dir}/{script_name}"

        try:
            # Upload the script.
            await ctx.client.upload_bytes(ctx.sandbox_id, code.encode("utf-8"), remote_script)

            # Execute via the sandbox's Python.
            result = await ctx.client.execute_command(
                ctx.sandbox_id,
                f"python3 {script_name}",
                cwd=ctx.project_dir,
                timeout=timeout,
            )

            output = result.result or ""
            if result.exit_code != 0:
                output += f"\n\nExit code: {result.exit_code}"

            # Clean up the temp script.
            try:
                await ctx.client.delete_file(ctx.sandbox_id, remote_script)
            except Exception:
                pass

            return output.strip() or "(no output)"

        except Exception as exc:
            return self._error(f"Sandbox Python execution failed: {exc}")

    async def _execute_locally(self, code: str, timeout: int) -> str:
        """Run Python code locally (OSS fallback)."""
        is_safe, reason = await get_guardian().check_command(code)
        if not is_safe:
            return self._error(f"Code blocked by Guardian: {reason}")

        jail_path = get_settings().file_jail_path
        jail_path.mkdir(parents=True, exist_ok=True)

        script_name = f"_pocketpaw_run_{uuid.uuid4().hex}.py"
        script_path = jail_path / script_name

        try:
            script_path.write_text(code, encoding="utf-8")
            result = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: subprocess.run(
                    [sys.executable, str(script_path)],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    cwd=str(jail_path),
                ),
            )
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\n\nExit code: {result.returncode}"
            return output.strip() or "(no output)"
        except subprocess.TimeoutExpired:
            return self._error(f"Python script timed out after {timeout}s")
        except Exception as e:
            return self._error(str(e))
        finally:
            if script_path.exists():
                try:
                    script_path.unlink()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# SyncToS3Tool — Daytona-aware
# ---------------------------------------------------------------------------


class DaytonaSyncToS3Tool(BaseTool):
    """Sync all files from the Daytona sandbox to S3 storage."""

    @property
    def name(self) -> str:
        return "sync_to_s3"

    @property
    def description(self) -> str:
        return (
            "Sync all files from the Daytona sandbox back to S3 storage. "
            "Call this after you finish your edit-run-verify loop to persist "
            "your changes."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        from pocketpaw_ee.cloud.daytona.context import resolve_daytona_context
        from pocketpaw_ee.cloud.daytona.router import (
            _sync_directory_from_sandbox_to_s3,
        )

        ctx = await resolve_daytona_context()
        if ctx is None:
            return self._error("No Daytona sandbox is active")

        try:
            await _sync_directory_from_sandbox_to_s3(
                ctx.client,
                ctx.sandbox_id,
                ctx.project_key,
                ctx.project_dir,
            )
            return "All files synced to S3"
        except Exception as exc:
            return self._error(f"Sync failed: {exc}")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_daytona_tools() -> list[BaseTool]:
    """Return all Daytona-aware tool instances.

    These have the SAME names as the OSS builtins (read_file, write_file,
    edit_file, list_dir, shell, run_python) and will REPLACE them in the
    ToolRegistry when registered. Safe to call on any install — the tools
    check for a sandbox context at execution time and fall back to local FS
    when none is found.

    ``sync_to_s3`` is an additional tool (no OSS counterpart) that syncs
    all sandbox files back to S3 storage.
    """
    return [
        DaytonaReadFileTool(),
        DaytonaWriteFileTool(),
        DaytonaEditFileTool(),
        DaytonaListDirTool(),
        DaytonaShellTool(),
        DaytonaRunPythonTool(),
        DaytonaSyncToS3Tool(),
    ]


__all__ = [
    "DaytonaReadFileTool",
    "DaytonaWriteFileTool",
    "DaytonaEditFileTool",
    "DaytonaListDirTool",
    "DaytonaShellTool",
    "DaytonaRunPythonTool",
    "DaytonaSyncToS3Tool",
    "get_daytona_tools",
]
