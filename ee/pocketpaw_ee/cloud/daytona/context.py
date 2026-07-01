"""Daytona context resolver — find the active sandbox for the current project.

At the heart of the Daytona-aware tool routing. When an agent is working on
a cloud project that has a provisioned Daytona sandbox, ``resolve_daytona_context``
returns a ``DaytonaContext`` with the client, sandbox ID, and project working
directory. Tools use this to route file/shell operations through the sandbox
instead of the local filesystem.

Resolution order:
  1. Explicit ``workspace_id`` / ``user_id`` / ``project_name`` (from tool params)
  2. Chat ContextVars (``current_workspace_id`` / ``current_user_id``) when
     running inside a cloud chat turn
  3. Workspace map scan — when no project_name is given but exactly one sandbox
     exists for the resolved workspace, use it

Returns ``None`` when no sandbox is found — the caller falls back to local FS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.daytona.client import DaytonaClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context value object
# ---------------------------------------------------------------------------


@dataclass
class DaytonaContext:
    """Resolved Daytona sandbox context for the current operation.

    Attributes:
        client: The Daytona SDK client wrapper.
        sandbox_id: The provisioned sandbox (VM) ID.
        project_dir: The sandbox directory where project files live
            (typically the user root, e.g. ``/home/daytona``).
        work_dir: The sandbox working directory (may differ from project_dir).
        project_key: The S3 storage key prefix
            (e.g. ``projects/{ws}/{uid}/{name}/``).
        project_name: The human-friendly project name.
    """

    client: DaytonaClient
    sandbox_id: str
    project_dir: str
    work_dir: str
    project_key: str
    project_name: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_key_for(workspace_id: str, user_id: str, project_name: str) -> str:
    """Build the S3 storage key prefix for a project."""
    return f"projects/{workspace_id}/{user_id}/{project_name}/"


def _extract_project_key_parts(project_key: str) -> tuple[str, str, str] | None:
    """Parse a project key into ``(workspace_id, user_id, project_name)``.

    Returns ``None`` if the key doesn't match the expected pattern.
    """
    parts = project_key.strip("/").split("/")
    if len(parts) >= 3 and parts[0] == "projects":
        return (parts[1], parts[2], "/".join(parts[3:]))
    return None


# ---------------------------------------------------------------------------
# ContextVar-based identity resolution (cloud chat context)
# ---------------------------------------------------------------------------


def _resolve_chat_identity() -> tuple[str | None, str | None]:
    """Read workspace_id and user_id from chat ContextVars.

    Returns ``(workspace_id, user_id)`` or ``(None, None)`` when not in a
    cloud chat run.
    """
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_user_id,
            current_workspace_id,
        )

        return (current_workspace_id(), current_user_id())
    except ImportError:
        return (None, None)


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------


async def resolve_daytona_context(
    workspace_id: str | None = None,
    user_id: str | None = None,
    project_name: str | None = None,
) -> DaytonaContext | None:
    """Resolve the active Daytona sandbox for the current context.

    Args:
        workspace_id: Explicit workspace ID. Falls back to chat ContextVars.
        user_id: Explicit user ID. Falls back to chat ContextVars.
        project_name: Explicit project name. When omitted, tries to auto-detect
            from the workspace map (single-sandbox shortcut).

    Returns:
        A ``DaytonaContext`` if a sandbox is found, or ``None``.
    """
    # ---- Resolve identity ----
    if not workspace_id or not user_id:
        ctx_ws, ctx_uid = _resolve_chat_identity()
        workspace_id = workspace_id or ctx_ws
        user_id = user_id or ctx_uid

    if not workspace_id:
        logger.debug("resolve_daytona_context: no workspace_id — no sandbox context")
        return None

    # ---- Resolve project_name ----
    project_key: str | None = None

    if project_name and user_id:
        project_key = _project_key_for(workspace_id, user_id, project_name)
    else:
        # No explicit project: scan workspace map for the first project that
        # has a sandbox for this workspace. This is the single-project shortcut.
        from pocketpaw_ee.cloud.daytona.store import list_project_keys_for_workspace

        keys = list_project_keys_for_workspace(workspace_id)
        if len(keys) == 0:
            logger.debug(
                "resolve_daytona_context: no sandboxed projects for workspace %s",
                workspace_id,
            )
            return None
        if len(keys) > 1:
            logger.debug(
                "resolve_daytona_context: multiple sandboxed projects for workspace %s "
                "and no project_name given — falling back to local FS",
                workspace_id,
            )
            return None
        project_key = keys[0]
        parts = _extract_project_key_parts(project_key)
        if parts:
            _, _, project_name = parts

    if not project_key or not project_name:
        return None

    # ---- Resolve sandbox ID ----
    from pocketpaw_ee.cloud.daytona.store import get_sandbox_id

    sandbox_id = get_sandbox_id(project_key)
    if not sandbox_id:
        logger.debug("resolve_daytona_context: no sandbox for project %s", project_key)
        return None

    # ---- Get sandbox details ----
    from pocketpaw_ee.cloud.daytona.client import get_daytona_client

    client = get_daytona_client()
    if client is None:
        logger.debug("resolve_daytona_context: Daytona not configured")
        return None

    try:
        project_dir = await client.get_project_dir(sandbox_id)
        work_dir = await client.get_work_dir(sandbox_id)
    except Exception as exc:
        logger.warning(
            "resolve_daytona_context: failed to get sandbox dirs for %s: %s",
            sandbox_id,
            exc,
        )
        return None

    return DaytonaContext(
        client=client,
        sandbox_id=sandbox_id,
        project_dir=project_dir,
        work_dir=work_dir,
        project_key=project_key,
        project_name=project_name,
    )


async def resolve_daytona_context_for_project(
    workspace_id: str, user_id: str, project_name: str
) -> DaytonaContext | None:
    """Convenience wrapper that takes all three identity params explicitly."""
    return await resolve_daytona_context(
        workspace_id=workspace_id,
        user_id=user_id,
        project_name=project_name,
    )


__all__ = [
    "DaytonaContext",
    "resolve_daytona_context",
    "resolve_daytona_context_for_project",
]
