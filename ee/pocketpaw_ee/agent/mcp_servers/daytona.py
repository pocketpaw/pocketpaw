"""Daytona sandbox MCP server — exposes file/shell/Python tools to the
claude_agent_sdk cloud chat backend that route operations through the
provisioned Daytona sandbox VM instead of the local filesystem.

When an agent is on the /code surface with a cloud project that has a Daytona
sandbox provisioned, this server's tools let the agent read, write, edit, list,
run shell commands, and execute Python code — all inside the sandbox VM.

The server is ambient (not opt-in). It checks for a sandbox context at each
tool call and returns a clear error when no sandbox is active so the agent
knows to fall back to local tools or the cloud REST API.

Tool ids namespace as ``mcp__pocketpaw_daytona__<tool>`` for allowlist matching.

Created: 2026-07-01
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_daytona"

# Tool IDs for allowlist matching.
READ_FILE_TOOL_ID = f"mcp__{SERVER_NAME}__read_file"
WRITE_FILE_TOOL_ID = f"mcp__{SERVER_NAME}__write_file"
EDIT_FILE_TOOL_ID = f"mcp__{SERVER_NAME}__edit_file"
LIST_DIR_TOOL_ID = f"mcp__{SERVER_NAME}__list_dir"
SHELL_TOOL_ID = f"mcp__{SERVER_NAME}__shell"
RUN_PYTHON_TOOL_ID = f"mcp__{SERVER_NAME}__run_python"
SYNC_TO_S3_TOOL_ID = f"mcp__{SERVER_NAME}__sync_to_s3"

DAYTONA_TOOL_IDS = (
    READ_FILE_TOOL_ID,
    WRITE_FILE_TOOL_ID,
    EDIT_FILE_TOOL_ID,
    LIST_DIR_TOOL_ID,
    SHELL_TOOL_ID,
    RUN_PYTHON_TOOL_ID,
    SYNC_TO_S3_TOOL_ID,
)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _error_response(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(data: Any) -> dict[str, Any]:
    payload = (
        data if isinstance(data, str) else json.dumps(data, default=str, separators=(",", ":"))
    )
    return {
        "content": [{"type": "text", "text": payload}],
    }


# ---------------------------------------------------------------------------
# Context resolver
# ---------------------------------------------------------------------------


async def _resolve_ctx() -> Any | None:
    """Resolve the Daytona sandbox context for the current chat turn."""
    from pocketpaw_ee.cloud.daytona.context import resolve_daytona_context

    return await resolve_daytona_context()


async def _require_ctx() -> Any:
    """Resolve context or raise a clear error message."""
    ctx = await _resolve_ctx()
    if ctx is None:
        raise ValueError(
            "No Daytona sandbox is active. "
            "Provision a sandbox via POST /api/v1/cloud/projects/<name>/workspace "
            "before using Daytona tools, or use local file/shell tools instead."
        )
    return ctx


# ---------------------------------------------------------------------------
# Path mapping helper
# ---------------------------------------------------------------------------


def _sandbox_path(ctx: Any, path: str) -> str:
    """Map a local or S3-key path to its sandbox equivalent."""
    from pocketpaw_ee.cloud.daytona.tools import _strip_project_prefix

    rel = _strip_project_prefix(path, ctx)
    return f"{ctx.project_dir}/{rel}".replace("//", "/")


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _read_file_handler(args: dict) -> dict:
    """Read a file from the Daytona sandbox."""
    path = args.get("path", "").strip()
    if not path:
        return _error_response("path is required")

    encoding = args.get("encoding", "utf-8")
    ctx = await _require_ctx()

    try:
        remote = _sandbox_path(ctx, path)
        data = await ctx.client.download_file(ctx.sandbox_id, remote)
        content = data.decode(encoding)
        if len(content) > 100_000:
            content = content[:100_000] + "\n\n...(truncated, file too large)"
        return _success_response(content)
    except Exception as exc:
        return _error_response(f"Failed to read file from sandbox: {exc}")


async def _write_file_handler(args: dict) -> dict:
    """Write a file in the Daytona sandbox."""
    path = args.get("path", "").strip()
    if not path:
        return _error_response("path is required")
    content = args.get("content", "")
    if not isinstance(content, str):
        return _error_response("content must be a string")

    ctx = await _require_ctx()

    try:
        remote = _sandbox_path(ctx, path)
        parent = os.path.dirname(remote)
        try:
            await ctx.client.create_folder(ctx.sandbox_id, parent)
        except Exception:
            pass
        await ctx.client.upload_bytes(ctx.sandbox_id, content.encode("utf-8"), remote)
        return _success_response({"ok": True, "path": path, "bytes": len(content)})
    except Exception as exc:
        return _error_response(f"Failed to write file in sandbox: {exc}")


async def _edit_file_handler(args: dict) -> dict:
    """Edit a file in the Daytona sandbox by replacing text."""
    path = args.get("path", "").strip()
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))

    if not path or not old_string:
        return _error_response("path and old_string are required")

    ctx = await _require_ctx()

    try:
        remote = _sandbox_path(ctx, path)
        data = await ctx.client.download_file(ctx.sandbox_id, remote)
        content = data.decode("utf-8")

        count = content.count(old_string)
        if count == 0:
            return _error_response("old_string not found in file")
        if not replace_all and count > 1:
            return _error_response(
                f"old_string appears {count} times. Provide more context or set replace_all=true"
            )

        new_content = content.replace(old_string, new_string)
        await ctx.client.upload_bytes(ctx.sandbox_id, new_content.encode("utf-8"), remote)

        replacements = count if replace_all else 1
        return _success_response({"ok": True, "path": path, "replacements": replacements})
    except Exception as exc:
        return _error_response(f"Failed to edit file in sandbox: {exc}")


async def _list_dir_handler(args: dict) -> dict:
    """List directory contents in the Daytona sandbox."""
    path = args.get("path", "").strip() or "."
    show_hidden = bool(args.get("show_hidden", False))

    ctx = await _require_ctx()

    try:
        remote = _sandbox_path(ctx, path)
        entries = await ctx.client.list_files(ctx.sandbox_id, remote)
        items = []
        for entry in entries:
            if not show_hidden and entry.name.startswith("."):
                continue
            items.append(
                {
                    "name": entry.name,
                    "is_dir": entry.is_dir,
                    "size": getattr(entry, "size", 0),
                }
            )
        return _success_response({"path": path, "files": items})
    except Exception as exc:
        return _error_response(f"Failed to list directory in sandbox: {exc}")


async def _shell_handler(args: dict) -> dict:
    """Execute a shell command in the Daytona sandbox."""
    command = args.get("command", "").strip()
    if not command:
        return _error_response("command is required")

    ctx = await _require_ctx()

    try:
        result = await ctx.client.execute_command(
            ctx.sandbox_id,
            command,
            cwd=ctx.project_dir,
            timeout=args.get("timeout", 120),
        )
        output = result.stdout or ""
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}"
        if result.exit_code != 0:
            output += f"\n\nExit code: {result.exit_code}"
        return _success_response(output.strip() or "(no output)")
    except Exception as exc:
        return _error_response(f"Sandbox command failed: {exc}")


async def _run_python_handler(args: dict) -> dict:
    """Execute Python code in the Daytona sandbox."""
    code = args.get("code", "").strip()
    if not code:
        return _error_response("code is required")

    timeout = int(args.get("timeout", 120))
    ctx = await _require_ctx()

    import uuid

    script_name = f"_paw_mcp_run_{uuid.uuid4().hex}.py"
    remote_script = f"{ctx.project_dir}/{script_name}"

    try:
        await ctx.client.upload_bytes(ctx.sandbox_id, code.encode("utf-8"), remote_script)

        result = await ctx.client.execute_command(
            ctx.sandbox_id,
            f"python3 {script_name}",
            cwd=ctx.project_dir,
            timeout=timeout,
        )

        output = result.stdout or ""
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}"
        if result.exit_code != 0:
            output += f"\n\nExit code: {result.exit_code}"

        try:
            await ctx.client.delete_file(ctx.sandbox_id, remote_script)
        except Exception:
            pass

        return _success_response(output.strip() or "(no output)")
    except Exception as exc:
        return _error_response(f"Sandbox Python execution failed: {exc}")


async def _sync_to_s3_handler(args: dict) -> dict:
    """Sync all sandbox files back to S3 storage."""
    ctx = await _require_ctx()

    try:
        from pocketpaw_ee.cloud.daytona.router import (
            _sync_directory_from_sandbox_to_s3,
        )

        # Timeout after 120s so the tool always returns even if a file
        # operation hangs. The sync uploads files incrementally, so partial
        # data is already persisted if we time out.
        await asyncio.wait_for(
            _sync_directory_from_sandbox_to_s3(
                ctx.client,
                ctx.sandbox_id,
                ctx.project_key,
                ctx.project_dir,
            ),
            timeout=120,
        )
        return _success_response({"ok": True, "message": "All files synced to S3"})
    except TimeoutError:
        return _success_response(
            {"ok": True, "message": "Sync timed out — partial data may have been synced"}
        )
    except Exception as exc:
        return _error_response(f"Sync failed: {exc}")


# ---------------------------------------------------------------------------
# Server builder
# ---------------------------------------------------------------------------


def build_daytona_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for Daytona sandbox tools.

    Returns ``(SERVER_NAME, server)`` or ``None`` when the Claude Agent SDK
    is not installed.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_daytona MCP disabled")
        return None

    @tool(
        "read_file",
        "Read the contents of a file from the Daytona sandbox VM. "
        "Use this when the project has a provisioned Daytona sandbox and you "
        "need to read a file. Args: `path` (required — path to the file, "
        "relative to the project root). Returns the file content as text.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (relative to project root)",
                },
                "encoding": {
                    "type": "string",
                    "description": "File encoding (default: utf-8)",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    async def read_file(args: dict) -> dict:  # type: ignore[no-untyped-def]
        return await _read_file_handler(args)

    @tool(
        "write_file",
        "Write content to a file in the Daytona sandbox VM. "
        "Creates the file and parent directories if they don't exist. "
        "Use this when the project has a provisioned Daytona sandbox and you "
        "need to create or overwrite a file. Args: `path` (required), "
        "`content` (required). Returns {ok, path, bytes}.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write (relative to project root)",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    )
    async def write_file(args: dict) -> dict:  # type: ignore[no-untyped-def]
        return await _write_file_handler(args)

    @tool(
        "edit_file",
        "Edit a file in the Daytona sandbox VM by replacing an exact string match "
        "with new content. Use when you need to make targeted edits to a file. "
        "Args: `path` (required), `old_string` (required), `new_string` (required), "
        "`replace_all` (optional bool). Returns {ok, path, replacements}.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit (relative to project root)",
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
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    )
    async def edit_file(args: dict) -> dict:  # type: ignore[no-untyped-def]
        return await _edit_file_handler(args)

    @tool(
        "list_dir",
        "List the contents of a directory in the Daytona sandbox VM. "
        "Args: `path` (required — path to the directory), "
        "`show_hidden` (optional bool, default false). "
        "Returns {path, files: [{name, is_dir, size}]}.",
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the directory to list (relative to project root)",
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "Show hidden files (default: false)",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    async def list_dir(args: dict) -> dict:  # type: ignore[no-untyped-def]
        return await _list_dir_handler(args)

    @tool(
        "shell",
        "Execute a shell command in the Daytona sandbox VM and return the output. "
        "Use this to run build commands, tests, install packages, etc. inside the "
        "sandbox. Args: `command` (required), `timeout` (optional int, default 120s). "
        "Returns the command's stdout + stderr.",
        {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default: 120)",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )
    async def shell(args: dict) -> dict:  # type: ignore[no-untyped-def]
        return await _shell_handler(args)

    @tool(
        "run_python",
        "Execute Python code in the Daytona sandbox VM and return the output. "
        "Use for data processing, calculations, or running installed packages inside "
        "the sandbox. Args: `code` (required — Python code to execute), "
        "`timeout` (optional int, default 120s). Returns stdout + stderr.",
        {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default: 120)",
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    )
    async def run_python(args: dict) -> dict:  # type: ignore[no-untyped-def]
        return await _run_python_handler(args)

    @tool(
        "sync_to_s3",
        "Sync all files from the Daytona sandbox back to S3 storage. "
        "Call this after you finish your edit-run-verify loop to persist "
        "your changes. No arguments needed.",
        {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    )
    async def sync_to_s3(args: dict) -> dict:  # type: ignore[no-untyped-def]
        return await _sync_to_s3_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[read_file, write_file, edit_file, list_dir, shell, run_python, sync_to_s3],
    )
    return SERVER_NAME, server


__all__ = [
    "SERVER_NAME",
    "DAYTONA_TOOL_IDS",
    "READ_FILE_TOOL_ID",
    "WRITE_FILE_TOOL_ID",
    "EDIT_FILE_TOOL_ID",
    "LIST_DIR_TOOL_ID",
    "SHELL_TOOL_ID",
    "RUN_PYTHON_TOOL_ID",
    "SYNC_TO_S3_TOOL_ID",
    "build_daytona_server",
]
